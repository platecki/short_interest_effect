from datetime import datetime, timedelta
import os
from airflow import DAG
from airflow.operators.python_operator import PythonOperator
from airflow.operators.dummy_operator import DummyOperator
from airflow.operators.custom_operators import VariableExistenceSensor
from airflow.models import Variable
from airflow import AirflowException
import lib.emrspark_lib as emrs
from airflow.configuration import conf as airflow_config

import logging

from airflow.utils import timezone
yesterday = timezone.utcnow() - timedelta(days=2)

from lib.common import *

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

dag = DAG('short_interests_dag',
          default_args=default_args,
          description="Pull short interest data from Quandl",
          schedule_interval='@daily',
          max_active_runs=1
)


def submit_spark_job_from_file(**kwargs):
    ec2, emr, iam = emrs.get_boto_clients(config['AWS']['REGION_NAME'], config=config)
    
    if emrs.is_cluster_terminated(emr, Variable.get('cluster_id', None)):
        Variable.set('short_interests_dag_state', 'FAILED')
        raise AirflowException("Cluster has been terminated. Redo all DAGs.")

    if Variable.get('prices_dag_state', None) == 'FAILED':
        Variable.set('short_interests_dag_state', 'FAILED')
        raise AirflowException("Error in prices_dag. Redo all DAGs.")

    cluster_dns = emrs.get_cluster_dns(emr, Variable.get('cluster_id'))
    emrs.kill_all_inactive_spark_sessions(cluster_dns)
    session_headers = emrs.create_spark_session(cluster_dns)
    helperspath = None
    if 'helperspath' in kwargs:
        helperspath = kwargs['helperspath']
    commonpath = None
    if 'commonpath' in kwargs:
        commonpath = kwargs['commonpath']
    emrs.wait_for_spark(cluster_dns, session_headers)
    job_response_headers = emrs.submit_spark_job_from_file(
        cluster_dns, session_headers, kwargs['filepath'],
        args=kwargs['args'],
        commonpath=commonpath,
        helperspath=helperspath)

    final_status, logs = emrs.track_spark_job(cluster_dns, job_response_headers)
    emrs.kill_spark_session(cluster_dns, session_headers)
    for line in logs:
        logging.info(line)
        if '(FAIL)' in str(line):
            logging.error(line)
            raise AirflowException("ETL process fails.")

    if final_status in ['available', 'ok'] and 'on_complete' in kwargs:
        kwargs['on_complete']()


# This is so that we don't end up re-running this DAG before everything else completes.
wait_for_fresh_run_task = VariableExistenceSensor(
    task_id='Wait_for_fresh_run',
    poke_interval=120,
    varnames=['short_interests_dag_state'],
    reverse=True,
    mode='reschedule',
    dag=dag
)

wait_for_cluster_task = VariableExistenceSensor(
    task_id='Wait_for_cluster',
    poke_interval=120,
    varnames=['cluster_id'],
    mode='reschedule',
    dag=dag
)

pull_stock_symbols_task = PythonOperator(
    task_id='Pull_stock_symbols',
    python_callable=submit_spark_job_from_file,
    op_kwargs={
        'commonpath': '{}/dags/etl/common.py'.format(airflow_dir),
        'helperspath': '{}/dags/etl/helpers.py'.format(airflow_dir),
        'filepath': '{}/dags/etl/pull_stock_info.py'.format(airflow_dir), 
        'args': {
            'AWS_ACCESS_KEY_ID': config['AWS']['AWS_ACCESS_KEY_ID'],
            'AWS_SECRET_ACCESS_KEY': config['AWS']['AWS_SECRET_ACCESS_KEY'],
            'START_DATE': config['App']['START_DATE'],
            'URL_NASDAQ': 'https://old.nasdaq.com/screening/companies-by-name.aspx?letter=0&exchange=nasdaq&render=download',
            'URL_NYSE': 'https://old.nasdaq.com/screening/companies-by-name.aspx?letter=0&exchange=nyse&render=download',
            'DB_HOST': 's3a://short-interest-effect',
            'TABLE_STOCK_INFO_NASDAQ': '/data/raw/stock_info_nasdaq',
            'TABLE_STOCK_INFO_NYSE': '/data/raw/stock_info_nyse',
        }
    },
    dag=dag
)

pull_short_interest_data_task = PythonOperator(
    task_id='Pull_short_interest_data',
    python_callable=submit_spark_job_from_file,
    op_kwargs={
        'commonpath': '{}/dags/etl/common.py'.format(airflow_dir),
        'helperspath': '{}/dags/etl/helpers.py'.format(airflow_dir),
        'filepath': '{}/dags/etl/pull_short_interests.py'.format(airflow_dir), 
        'args': {
            'START_DATE': config['App']['START_DATE'],
            'QUANDL_API_KEY': config['Quandl']['API_KEY'],
            'YESTERDAY_DATE': '{{yesterday_ds}}',
            'LIMIT': LIMIT,
            'STOCKS': STOCKS,
            'AWS_ACCESS_KEY_ID': config['AWS']['AWS_ACCESS_KEY_ID'],
            'AWS_SECRET_ACCESS_KEY': config['AWS']['AWS_SECRET_ACCESS_KEY'],
            'DB_HOST': config['App']['DB_HOST'],
            'TABLE_STOCK_INFO_NASDAQ': config['App']['TABLE_STOCK_INFO_NASDAQ'],
            'TABLE_STOCK_INFO_NYSE': config['App']['TABLE_STOCK_INFO_NYSE'],
            'TABLE_SHORT_INTERESTS_NASDAQ': config['App']['TABLE_SHORT_INTERESTS_NASDAQ'],
            'TABLE_SHORT_INTERESTS_NYSE': config['App']['TABLE_SHORT_INTERESTS_NYSE'],
        }
    },
    dag=dag
)

quality_check_task = PythonOperator(
    task_id='Quality_check',
    python_callable=submit_spark_job_from_file,
    op_kwargs={
        'commonpath': '{}/dags/etl/common.py'.format(airflow_dir),
        'helperspath': '{}/dags/etl/helpers.py'.format(airflow_dir),
        'filepath': '{}/dags/etl/pull_short_interests_quality.py'.format(airflow_dir), 
        'args': {
            'AWS_ACCESS_KEY_ID': config['AWS']['AWS_ACCESS_KEY_ID'],
            'AWS_SECRET_ACCESS_KEY': config['AWS']['AWS_SECRET_ACCESS_KEY'],
            'YESTERDAY_DATE': '{{yesterday_ds}}',
            'STOCKS': STOCKS,
            'DB_HOST': config['App']['DB_HOST'],
            'TABLE_STOCK_INFO_NASDAQ': config['App']['TABLE_STOCK_INFO_NASDAQ'],
            'TABLE_STOCK_INFO_NYSE': config['App']['TABLE_STOCK_INFO_NYSE'],
            'TABLE_SHORT_INTERESTS_NASDAQ': config['App']['TABLE_SHORT_INTERESTS_NASDAQ'],
            'TABLE_SHORT_INTERESTS_NYSE': config['App']['TABLE_SHORT_INTERESTS_NYSE'],
        },
        'on_complete': lambda *args: Variable.set('short_interests_dag_state', 'COMPLETED')
    },
    dag=dag
)


wait_for_fresh_run_task >> wait_for_cluster_task >> \
pull_stock_symbols_task >> pull_short_interest_data_task >> quality_check_task