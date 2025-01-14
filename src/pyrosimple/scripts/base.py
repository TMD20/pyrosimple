""" Command Line Script Support.

    Copyright (c) 2009, 2010 The PyroScope Project <pyroscope.project@gmail.com>
"""


import errno
import logging.config
import sys
import textwrap
import time
import traceback

from argparse import ArgumentParser
from typing import Iterator, List

from pyrosimple import config, error, io
from pyrosimple.torrent import rtorrent
from pyrosimple.util import fmt, pymagic


class ScriptBase:
    """Base class for command line interfaces."""

    # log level for user-visible standard logging
    STD_LOG_LEVEL = logging.INFO

    # argument description for the usage information
    ARGS_HELP = "<log-base>..."

    # additonal stuff appended after the command handler's docstring
    ADDITIONAL_HELP: List[str] = []

    # Can be empty or None in derived classes
    COPYRIGHT = ""

    @classmethod
    def setup(cls, _=None):
        """Set up the runtime environment."""
        logging.basicConfig(level=logging.WARNING)

    def __init__(self):
        """Initialize CLI."""
        self.startup = time.time()
        self.LOG = pymagic.get_class_logger(self)

        self.args = None
        self.options = None
        self.return_code = 0
        self.engine = None
        self.intermixed_args = False

        # For python 3.7 compatibility
        try:
            import importlib.metadata  # pylint: disable=import-outside-toplevel

            self.__version__ = importlib.metadata.version(  # pylint: disable=no-member
                "pyrosimple"
            )
        except ImportError:
            self.__version__ = "unknown"
        self.version_info = f"{self.__version__} on Python {sys.version.split()[0]}"

        self.parser = ArgumentParser(
            usage="%(prog)s [options] " + self.ARGS_HELP + "\n\n"
            "%(prog)s "
            + self.version_info
            + ("\n" + self.COPYRIGHT if self.COPYRIGHT else "")
            + "\n\n"
            + textwrap.dedent(self.__doc__.rstrip()).lstrip("\n")
            + "\n".join(self.ADDITIONAL_HELP)
            + "\n\nFor more details, see the full documentation at"
            + "\n\n    https://kannibalox.github.io/pyrosimple/",
        )

        self.parser.add_argument(
            "--version", action="version", version="%(prog)s " + self.version_info
        )
        self.parser.add_argument("args", nargs="*")

    def add_bool_option(self, *args, **kwargs):
        """Add a boolean option.

        @keyword help: Option description.
        """
        dest = (
            [o for o in args if o.startswith("--")][0]
            .replace("--", "")
            .replace("-", "_")
        )
        self.parser.add_argument(
            dest=dest, action="store_true", default=False, help=kwargs["help"], *args
        )

    def add_value_option(self, *args, **kwargs):
        """Add a value option.

        @keyword dest: Destination attribute, derived from long option name if not given.
        @keyword action: How to handle the option.
        @keyword help: Option description.
        @keyword default: If given, add this value to the help string.
        """
        kwargs["metavar"] = args[-1]
        if "dest" not in kwargs:
            kwargs["dest"] = (
                [o for o in args if o.startswith("--")][0]
                .replace("--", "")
                .replace("-", "_")
            )
        if "default" in kwargs and kwargs["default"]:
            kwargs["help"] += f" [{kwargs['default']}]"
        if "choices" in kwargs:
            del kwargs["type"]
        self.parser.add_argument(*args[:-1], **kwargs)

    def get_options(self):
        """Get program options."""
        self.parser.add_argument(
            "-q",
            "--quiet",
            "--cron",  # For backwards compatibility
            help="silence warnings",
            dest="log_level",
            action="store_const",
            const=logging.CRITICAL,
        )
        self.parser.add_argument(
            "--debug",
            help="show detailed messages",
            dest="log_level",
            action="store_const",
            const=logging.DEBUG,
        )
        self.parser.add_argument(
            "-v",
            "--verbose",
            help="show additional information",
            dest="log_level",
            action="store_const",
            const=logging.INFO,
        )

        # Template method to add options of derived class
        self.add_options()

        if self.intermixed_args:
            self.options = self.parser.parse_intermixed_args()
        else:
            self.options = self.parser.parse_args()
        self.args = self.options.args

        if self.options.log_level:
            logging.getLogger().setLevel(self.options.log_level)

        self.LOG.debug(
            "Options: %s",
            ", ".join("%s=%r" % i for i in sorted(vars(self.options).items())),
        )

    def fatal(self, msg, exc=None):
        """Exit on a fatal error."""
        if exc is not None:
            self.LOG.fatal("%s (%s)", msg, exc)
            if logging.getLogger().isEnabledFor(logging.DEBUG):
                return  # let the caller re-raise it
        else:
            self.LOG.fatal(msg)
        sys.exit(error.EX_SOFTWARE)

    def run(self):
        """The main program skeleton."""
        log_total = True

        try:
            try:
                # Preparation steps
                self.get_options()

                # Template method with the tool's main loop
                self.mainloop()
            except error.LoggableError:
                print(traceback.format_exc())
                sys.exit(error.EX_SOFTWARE)
            except KeyboardInterrupt:
                print()
                self.LOG.critical("Aborted by CTRL-C!\n")
                sys.exit(error.EX_TEMPFAIL)
            except OSError as exc:
                # [Errno 32] Broken pipe?
                if exc.errno == errno.EPIPE:
                    print(f"\n{exc}, exiting!\n", file=sys.stderr)
                    sys.exit(error.EX_IOERR)
                else:
                    raise
        finally:
            # Shut down
            if log_total and self.options:  ## No time logging on --version and such
                running_time = time.time() - self.startup
                self.LOG.log(
                    self.STD_LOG_LEVEL, "Total time: %.3f seconds.", running_time
                )
            logging.shutdown()

        # Special exit code?
        if self.return_code:
            sys.exit(self.return_code)

    def add_options(self):
        """Add program options."""

    def mainloop(self):
        """The main loop."""
        raise NotImplementedError()

    # pylint: disable=no-self-use
    def rpc_stats(self) -> str:
        """Return a string with RPC statistics"""
        req_num = int(io.scgi.request_counter._value.get())
        req_sz = fmt.human_size(io.scgi.request_size_counter._value.get())
        req_time = round(
            list(io.scgi.response_time_summary._child_samples())[1].value, 3
        )
        resp_sz = fmt.human_size(io.scgi.response_size_counter._value.get())
        return f"{req_num} requests ({req_sz}) in {req_time}s (repsonse {resp_sz})"


class ScriptBaseWithConfig(ScriptBase):  # pylint: disable=abstract-method
    """CLI tool with configuration support."""

    def add_options(self):
        super().add_options()
        self.parser.add_argument("-U", "--url", help="URL to rtorrent instance")

    def get_options(self):
        """Get program options."""
        super().get_options()
        if self.options.url:
            config.settings["SCGI_URL"] = self.options.url
        config.load_custom_py()
        self.engine = rtorrent.RtorrentEngine()

    def lookup_connection_alias(self, url: str) -> str:  # pylint: disable=no-self-use
        """Convert a connection alias to the actual URL (if set in the config"""
        if url in config.settings["CONNECTIONS"]:
            return str(config.settings["CONNECTIONS"][url])
        return url

    def multi_connection_lookup(self, url: str) -> Iterator[str]:
        """Return a list of host URLs.

        This is separate from lookup_connection_alias due to scripts needing to be written specifically
        to handle this"""
        val = config.settings["CONNECTIONS"].get(url, [url])
        if isinstance(val, list):
            for v in val:
                yield self.lookup_connection_alias(v)
        else:
            yield self.lookup_connection_alias(val)
