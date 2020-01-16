""" Cluster DAG

This DAG deals with cluster creation and then wait for the other DAGs to complete
before terminating all created objects, including EMR cluster, key pairs,
and security groups.
"""
import os
from datetime import timedelta
from airflow import DAG
from airflow.operators.python_operator import PythonOperator
from airflow.operators.custom_operators import VariableExistenceSensor
from airflow.models import Variable
import lib.emrspark_lib as emrs
import configparser
import time

import logging
import os

from airflow.utils import timezone
yesterday = timezone.utcnow() - timedelta(days=2)

config = configparser.ConfigParser()
config.read('airflow/config.cfg')

CLUSTER_NAME = config['AWS']['CLUSTER_NAME']
VPC_ID = config['AWS']['VPC_ID']
SUBNET_ID = config['AWS']['SUBNET_ID']


default_args = {
    'owner': 'jaycode',
    'start_date': yesterday,
    'depends_on_past': True,
    'retries': 0,
    'email_on_retry': False,

    # Catch up is True because we want the operations to be atomic i.e. if I
    # skipped running the DAGs for a few days I'd want this system to run
    # for all these missing dates.
    'catchup':True
}

dag = DAG('cluster_dag',
          default_args=default_args,
          description='EMR Cluster-related actions',
          schedule_interval='@daily',
          max_active_runs=1
)


ec2, emr, iam = emrs.get_boto_clients(config['AWS']['REGION_NAME'], config=config)

if VPC_ID == '':
    VPC_ID = emrs.get_first_available_vpc(ec2)

if SUBNET_ID == '':
    SUBNET_ID = emrs.get_first_available_subnet(ec2, VPC_ID)


def preparation(**kwargs):
    Variable.delete('cluster_id')
    Variable.delete('keypair_name')
    Variable.delete('master_sg_id')
    Variable.delete('slave_sg_id')
    Variable.delete('short_interests_dag_state')
    Variable.delete('prices_dag_state')
    Variable.delete('combine_dag_state')

    master_sg_id = emrs.create_security_group(ec2, '{}SG'.format(CLUSTER_NAME),
        'Master SG for {}'.format(CLUSTER_NAME), VPC_ID)
    slave_sg_id = emrs.create_security_group(ec2, '{}SlaveSG'.format(CLUSTER_NAME),
        'Slave SG for {}'.format(CLUSTER_NAME), VPC_ID)

    Variable.set('master_sg_id', master_sg_id)
    Variable.set('slave_sg_id', slave_sg_id)

    keypair = emrs.recreate_key_pair(ec2, '{}_pem'.format(CLUSTER_NAME))
    Variable.set('keypair_name', keypair['KeyName'])

    emrs.recreate_default_roles(iam)


def create_cluster(**kwargs):
    cluster_id = emrs.create_emr_cluster(emr, CLUSTER_NAME,
        Variable.get('master_sg_id'),
        Variable.get('slave_sg_id'),
        Variable.get('keypair_name'), SUBNET_ID)
        # release_label='emr-5.28.1')
    Variable.set('cluster_id', cluster_id)


def terminate_cluster(**kwargs):
    if Variable.get('combine_dag_state') == '':
        emrs.delete_cluster(emr, Variable.get('cluster_id'))


def cleanup(**kwargs):
    ec2.delete_key_pair(KeyName=Variable.get('keypair_name'))
    emrs.delete_security_group(ec2, Variable.get('master_sg_id'))
    time.sleep(2)
    emrs.delete_security_group(ec2, Variable.get('slave_sg_id'))
    Variable.delete('cluster_id')
    Variable.delete('keypair_name')
    Variable.delete('master_sg_id')
    Variable.delete('slave_sg_id')
    Variable.delete('short_interests_dag_state')
    Variable.delete('prices_dag_state')
    Variable.delete('combine_dag_state')


preparation_task = PythonOperator(
    task_id='Preparation',
    python_callable=preparation,
    dag=dag
)

create_cluster_task = PythonOperator(
    task_id='Create_cluster',
    python_callable=create_cluster,
    dag=dag
)

check_etl_completion_task = VariableExistenceSensor(
    task_id='Check_etl_completion',
    poke_interval=120,
    varnames=['combine_dag_state'],
    mode='reschedule',
    dag=dag
)

terminate_cluster_task = PythonOperator(
    task_id='Terminate_cluster',
    python_callable=terminate_cluster,
    dag=dag
)

cleanup_task = PythonOperator(
    task_id='Cleanup',
    python_callable=cleanup,
    dag=dag
)

preparation_task >> create_cluster_task >> \
check_etl_completion_task >> terminate_cluster_task >> \
cleanup_task