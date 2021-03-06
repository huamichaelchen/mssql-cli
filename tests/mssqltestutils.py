from datetime import datetime, timedelta
import os
import socket
import time
from argparse import Namespace
import mssqlcli.sqltoolsclient as sqltoolsclient
import mssqlcli.mssqlcliclient as mssqlcliclient
from mssqlcli.mssql_cli import MssqlCli
from mssqlcli.config import get_config
from mssqlcli.mssqlclioptionsparser import create_parser
from utility import random_str


_BASELINE_DIR = os.path.dirname(os.path.abspath(__file__))

# test queries mapped to files
test_queries = [
    ("SELECT 1", 'small.txt'),
    ("SELECT 1; SELECT 2;", 'multiple.txt'),
    ("SELECT %s" % ('x' * 250), 'col_too_wide.txt'),
    ("SELECT REPLICATE(CAST('X,' AS VARCHAR(MAX)), 1024)", 'col_wide.txt')
]

def create_mssql_cli(**non_default_options):
    mssqlcli_options = create_mssql_cli_options(**non_default_options)
    mssql_cli = MssqlCli(mssqlcli_options)

    return mssql_cli

def create_mssql_cli_client(options=None, owner_uri=None, connect=True, sql_tools_client=None,
                            **additional_params):
    """
    Retrieve a mssqlcliclient connection.
    :param options: options
    :param owner_uri: string
    :param connect: boolean
    :param sql_tools_client: SqlToolsClient
    :param additional_params: kwargs
    :return: MssqlCliClient
    """
    try:
        sql_tools_client = sql_tools_client if sql_tools_client else sqltoolsclient.SqlToolsClient()
        mssql_cli_options = options if options else create_mssql_cli_options()

        mssql_cli_client = mssqlcliclient.MssqlCliClient(mssql_cli_options,
                                                         sql_tools_client,
                                                         owner_uri=owner_uri,
                                                         **additional_params)

        if connect:
            mssql_cli_client.connect_to_database()
        return mssql_cli_client
    except Exception as e:
        print('Connection failed')
        raise e

def create_mssql_cli_options(**nondefault_options):

    parser = create_parser()

    default_mssql_cli_options = parser.parse_args('')

    if nondefault_options:
        updateable_mssql_cli_options = vars(default_mssql_cli_options)
        for option, value in nondefault_options.items():
            if option not in updateable_mssql_cli_options.keys():
                raise Exception('Invalid mssql-cli option specified: {}'.format(option))

            updateable_mssql_cli_options[option] = value

        return Namespace(**updateable_mssql_cli_options)

    return default_mssql_cli_options

def create_mssql_cli_config(options=None):
    """
    Create config from options.
    """
    if not options:
        options = create_mssql_cli_options()
    return get_config(options.mssqlclirc_file)

def shutdown(connection):
    connection.shutdown()

def getTempPath(*args):
    testRoot = os.path.join(os.path.abspath(__file__), '..')
    tempPath = os.path.join(testRoot, 'temp')
    for arg in args:
        tempPath = os.path.join(tempPath, arg)
    return  os.path.abspath(tempPath)

def create_test_db():
    """
    Creates database for test, using various status checks and retry logic for reliability.
    - Calls helper method to check status of create db task, if possible
    - Exits on successful response or retry period exceeds time limit
    """
    options = create_mssql_cli_options(database='master')
    client = create_mssql_cli_client(options)

    local_machine_name = socket.gethostname().replace(
        '-', '_').replace('.', '_')

    test_db_name = u'mssqlcli_testdb_{0}_{1}'.format(
        local_machine_name, random_str())
    query_db_create = u"CREATE DATABASE {0};".format(test_db_name)

    try:
        for _, _, status, _, is_create_error in client.execute_query(query_db_create):
            if _is_client_db_on_cloud(client):
                # retry logic is only supported for sql azure
                create_db_status, create_db_error = _check_create_db_status(test_db_name, client)

                if create_db_status == 'FAILED':
                    # break loop to assert db creation failure
                    raise AssertionError("Database creation failed. Retry logic for SQL " \
                                        "Azure DB was unsuccessful with the following error: " \
                                        "\n{}".format(create_db_error))

            if is_create_error:
                # break loop to assert db creation failure
                raise AssertionError("Database creation failed: {}".format(status))

        return test_db_name

    finally:
        shutdown(client)

def _is_client_db_on_cloud(client):
    """
    Checks if client is connected to Azure DB.
    """
    for rows, _, _, _, _ in client.execute_query("SELECT @@VERSION"):
        if "microsoft sql azure" in rows[0][0].lower():
            return True

    return False

def _check_create_db_status(db_name, client):
    """
    Uses retry logic with sys.dm_operation_status to check statis of create database job.
    """
    query_check_status = u"SELECT TOP 1 state_desc, error_desc FROM sys.dm_operation_status " \
                         u"WHERE major_resource_id = '{}' AND operation = 'CREATE DATABASE' " \
                         u"ORDER BY start_time DESC".format(db_name)

    # retry for 5 minutes until db status is no longer 'processing'
    datetime_end_loop = datetime.now() + timedelta(minutes=5)
    state_desc, error_desc = None, None
    while datetime.now() < datetime_end_loop:
        for row, _, status, _, is_error in client.execute_query(query_check_status):
            if is_error:
                raise ConnectionError("Checking database creation status failed: {}"\
                                      .format(status))
            state_desc = row[0][0]
            error_desc = row[0][1]

        if state_desc != 'PROCESSING':
            break

        # call sleep so db isn't overburdened with requests
        time.sleep(5)

    return (state_desc, error_desc)

def clean_up_test_db(test_db_name):
    client = create_mssql_cli_client()
    query = u"DROP DATABASE {0};".format(test_db_name)
    success = True
    for _, _, _, _, is_error in client.execute_query(query):
        if is_error is True:
            success = False
    shutdown(client)
    return success

def get_file_contents(file_path):
    """ Get expected result from file. """
    try:
        with open(file_path, 'r') as f:
            # remove string literals (needed in python2) and newlines
            return f.read().replace('\r', '').strip()
    except OSError as e:
        raise e

def get_io_paths(test_file_suffix):
    """ Returns tuple of file paths for the input and output of a test. """
    i = os.path.join(_BASELINE_DIR, 'test_query_inputs', 'input_%s' % test_file_suffix)
    o = os.path.join(_BASELINE_DIR, 'test_query_baseline', 'baseline_%s' % test_file_suffix)
    return (i, o)
