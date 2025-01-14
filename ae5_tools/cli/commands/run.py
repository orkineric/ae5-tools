import click

from ..login import cluster_call, login_options
from ..utils import add_param
from ..format import format_options
from ...identifier import Identifier


@click.group(short_help='delete, info, list, log, stop',
             epilog='Type "ae5 run <command> --help" for help on a specific command.')
@format_options()
@login_options()
def run():
    '''Commands related to run records.'''
    pass


@run.command()
@click.argument('run', required=False)
@format_options()
@login_options()
def list(run):
    '''List all available run records.

       By default, lists all runs visible to the authenticated user.
       Simple filters on owner, run name, or id can be performed by
       supplying an optional RUN argument. Filters on other fields may
       be applied using the --filter option.
    '''
    if run:
        ident = Identifier.from_string(run, no_revision=True)
        add_param('filter', ident.project_filter())
    cluster_call('run_list', cli=True)


@run.command()
@click.argument('run')
@format_options()
@login_options()
def info(run):
    '''Retrieve information about a single run.

       The RUN identifier need not be fully specified, and may even include
       wildcards. But it must match exactly one run.
    '''
    cluster_call('run_info', run, cli=True)


@run.command(short_help='Retrieve the log for a single run.')
@click.argument('run')
@format_options()
@login_options()
def log(run):
    '''Retrieve the log file for a particular run.

       The RUN identifier need not be fully specified, and may even include
       wildcards. But it must match exactly one run.
    '''
    cluster_call('run_log', run, cli=True)


@run.command()
@click.argument('run')
@click.option('--yes', is_flag=True, help='Do not ask for confirmation.')
@format_options()
@login_options()
def stop(run, yes):
    '''Stop a run.

       Does not produce an error if the run has already completed.

       The RUN identifier need not be fully specified, and may even include
       wildcards. But it must match exactly one run.
    '''
    cluster_call('run_stop', ident=run,
                 confirm=None if yes else 'Stop run {ident}',
                 prefix='Stopping run {ident}...',
                 postfix='stopped.', cli=True)


@run.command()
@click.argument('run')
@click.option('--yes', is_flag=True, help='Do not ask for confirmation.')
@format_options()
@login_options()
def delete(run, yes):
    '''Delete a run record.

       The RUN identifier need not be fully specified, and may even include
       wildcards. But it must match exactly one run.
    '''
    cluster_call('run_delete', ident=run,
                 confirm=None if yes else 'Delete run {ident}',
                 prefix='Deleting run {ident}...',
                 postfix='deleted.', cli=True)
