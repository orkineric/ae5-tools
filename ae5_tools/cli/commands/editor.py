import click

from ..login import login_options, cluster_call
from ..format import format_options


@click.group(short_help='info, list',
             epilog='Type "ae5 editor <command> --help" for help on a specific command.')
@format_options()
@login_options()
def editor():
    '''Commands related to development editors.'''
    pass


@editor.command()
@format_options()
@login_options()
def list():
    '''List the available editors.
    '''
    cluster_call('editor_list', cli=True)


@editor.command()
@click.argument('name')
@format_options()
@login_options()
def info(name):
    '''Retrieve the record of a single editor.

       The NAME identifier must match exactly one name of an editor.
       Wildcards may be included.
    '''
    cluster_call('editor_info', name, cli=True)
