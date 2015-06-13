"""
This module defines a set of commands (both uWSGI Master FIFO commands and
:program:`manage.py` subcommands) that a server can run on itself.

This module reads from the following environment variables:

.. envvar:: UWSGI_MASTER_FIFO
    The uWSGI Master FIFO for the parent Django project.

.. envvar:: DJANGO_SETTINGS_MODULE
    The settings module of the parent Django project. The module must define the
    variable :attr:`BASE_DIR` to be the filepath of the root directory of the
    Django project because this module uses it to determine the filepath of the
    :program:`manage.py` file to call.

Both of these variable are defined in the :obj:`.env` file generated by
:program:`setup_environment.sh`.
"""

import os
import stat
import select
import logging
import importlib
import subprocess
from rest_framework.exceptions import APIException
from control.exceptions import (
    InvalidFifoPath, InvalidFifoFile, InvalidManagerPath
)

fifo_path = os.environ['UWSGI_MASTER_FIFO']
django_settings = importlib.import_module(os.environ['DJANGO_SETTINGS_MODULE'])
manager_path = os.path.join(django_settings.BASE_DIR, 'manage.py')
logger = logging.getLogger('cityfarm_api.control')


###################
# Command Classes #
###################

class OutputItem(str):
    """
    A line or set of lines in the output of a command. An
    :class:`~control.command.OutputItem` is just a :class:`str` with an extra
    attribute :attr:`is_error` that reflects whether or not this item resulted
    from an error in it's source program.
    """
    def __new__(cls, text, is_error=False):
        obj = super().__new__(cls, text)
        obj.is_error = is_error
        return obj


class Command:
    """
    A base class for all commands in this module. Subclasses of
    :class:`~control.commands.Command` should define an attribute
    :attr:`returncode` after their :func:`~control.commands.Command.run`
    function has been called reflecting the result of the command that was run.
    They should also define an attribute :attr:`title` that is the name of the
    command being run.
    """
    #: The title of this command
    title = 'Unnamed Command'

    def run(self):
        """
        Runs this command and generates an :class:`~control.commands.OutputItem`
        instance for each item in the output of the command. Subclasses of
        :class:`~control.commands.Command` must implement
        :func:`~control.commands.Command.run`.
        """
        raise NotImplementedError()

    def to_json(self):
        """
        Renders the result of this command as JSON. The response has the keys
        'title' (the title of this command), 'log' (the entire log of the
        command as a string), 'error' (the error messages returned by the
        command), and 'returncode' (the return code of the command).
        """
        log = []
        for item in self.run():
            logger.debug(item)
            log.append(item)
        error = [item for item in log if item.is_error]
        response = {
            'title': self.title,
            'log': ''.join(log),
            'error': ''.join(error),
            'returncode': self.returncode
        }
        return response


class FifoCommand(Command):
    """
    Base class for any commands that write a command to the uWSGI Master FIFO.
    Inherits from :class:`~control.commands.Command`. Subclasses of this command
    must define the attribute :attr:`fifo_command`, which is the command to
    write to the FIFO.
    """
    @classmethod
    def check(self):
        """
        Ensures that the uWSGI Master FIFO to write to exists and is a named
        pipe. Throws the appropriate exception if these conditions are not met.
        """
        if not os.path.exists(fifo_path):
            raise InvalidFifoPath(fifo_path)
        if not stat.S_ISFIFO(os.stat(fifo_path).st_mode):
            raise InvalidFifoFile(fifo_path)

    def on_failure(self):
        """
        This function will be called by
        :func:`~control.commands.FifoCommand.run` if it fails to write to the
        FIFO. This allows subclasses to handle failure differently depending on
        how important their functionality is.
        """
        return
        yield

    def run(self):
        self.check()
        try:
            f = os.open(fifo_path, os.O_WRONLY | os.O_NONBLOCK)
            os.write(f, self.fifo_command)
            os.close(f)
        except OSError:
            yield OutputItem('Failed to write to FIFO\n', True)
            self.returncode = 1
            yield from self.on_failure()
        else:
            yield OutputItem(
                'Wrote {} to "{}"'.format(self.fifo_command, fifo_path),
            )
            self.returncode = 0
        logger.debug('{} returned {}'.format(self.title, self.returncode))


class ReloadWorkers(FifoCommand):
    """ Chain reload the uWSGI workers. """
    title = 'Reload Workers'
    fifo_command = b'c'

    def on_failure(self):
        yield OutputItem(
            'We are probably not running behind uWSGI. Assuming Django '
            'development server and attempting touch reload.', True
        )
        if os.environ['RUN_MAIN'] == 'true':
            touch_reload = TouchReload()
            yield from touch_reload.run()
            self.returncode = touch_reload.returncode
        else:
            yield OutputItem(
                'We are running behind neither uWSGI nor the Django '
                'development server, so I don\'t know how to restart. Manually '
                'restart the server for changes to take effect.', True
            )


class ShellCommand(Command):
    """
    Abstract base class for any commands that perform a command in the console.
    Subclasses of this class must define the attribute :attr:`command`, which is
    the command to run as a string.
    """
    @classmethod
    def check(self):
        """
        Ensures that the :program:`manage.py` file to call exists. Throws an
        :exception:`~control.exceptions.InvalidManagerPath` exception if not.
        """
        if not os.path.isfile(manager_path):
            raise InvalidManagerPath(manager_path)

    def run(self):
        self.check()
        logger.debug('Running shell command "{}"'.format(self.command))
        proc = subprocess.Popen(
            self.command,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        while True:
            reads = [proc.stdout.fileno(), proc.stderr.fileno()]
            ret = select.select(reads, [], [])
            for fd in ret[0]:
                if fd == proc.stdout.fileno():
                    item = b''.join(proc.stdout.readlines()).decode('ascii')
                    yield OutputItem(item)
                if fd == proc.stderr.fileno():
                    item = b''.join(proc.stderr.readlines()).decode('ascii')
                    yield OutputItem(item, True)
            if proc.poll() is not None:
                self.returncode = proc.returncode
                break
        logger.debug('{} returned {}'.format(self.title, self.returncode))


class TouchReload(ShellCommand):
    """ Touch the manage.py file to trigger a Django code reload """
    title = 'Touch Reload'
    command = ' '.join(['touch', manager_path])


class ManagerCommand(ShellCommand):
    """
    Base class for any commands that perform a subcommand of the Django
    :program:`manage.py` file. Subclasses of this class must define the
    attribute :attr:`args`, which is a list of arguments to pass to the
    :program:`manage.py` file.
    """
    @property
    def command(self):
        return ' '.join(('python', manager_path) + self.args)


class MakeMigrations(ManagerCommand):
    """ Create new migrations(s) for apps """
    title = 'Make Migrations'
    args = ('makemigrations',)


class Migrate(ManagerCommand):
    """ Update database schema """
    title = 'Migrate'
    args = ('migrate',)
