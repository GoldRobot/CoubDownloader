#!/usr/bin/env python3

"""
Copyright (C) 2018-2020 HelpSeeker <AlmostSerious@protonmail.ch>

This file is part of CoubDownloader.

CoubDownloader is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

CoubDownloader is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with CoubDownloader.  If not, see <https://www.gnu.org/licenses/>.
"""

import asyncio
import os
import subprocess
import sys

from ssl import SSLCertVerificationError, SSLContext
from textwrap import dedent

import urllib.error
from urllib.request import urlopen

import aiohttp

from utils import container
from utils import download
import utils.messaging as msg
from utils.options import parse_cli, ConfigError

# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~
# Global Variables
# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~

ERROR_DEP = 1     # missing required software
ERROR_OPT = 2     # invalid user-specified option
ERROR_RUN = 3     # misc. runtime error
ERROR_DOWN = 4    # failed to download all input links (existence == success)
ERROR_INT = 5     # early termination was requested by the user (i.e. Ctrl+C)
ERROR_CONN = 6    # connection either couldn't be established or was lost

PADDING = 5

SSLCONTEXT = SSLContext()

ENV = dict(os.environ)
# Change library search path based on script usage
# https://pyinstaller.readthedocs.io/en/stable/runtime-information.html#ld-library-path-libpath-considerations
if hasattr(sys, 'frozen') and hasattr(sys, '_MEIPASS'):
    lp_key = 'LD_LIBRARY_PATH'  # for GNU/Linux and *BSD.
    lp_orig = ENV.get(lp_key + '_ORIG')
    if lp_orig is not None:
        ENV[lp_key] = lp_orig
    else:
        ENV.pop(lp_key, None)   # LD_LIBRARY_PATH was not set

opts = None

# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~
# Functions
# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~

def check_prereq():
    """Test if all required 3rd-party tools are installed."""
    try:
        subprocess.run([opts.ffmpeg_path],
                       stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL,
                       env=ENV, check=False)
    except FileNotFoundError:
        msg.err("Error: FFmpeg not found!", color=msg.ERROR)
        sys.exit(ERROR_DEP)


def check_connection():
    """Check if user can connect to coub.com."""
    try:
        urlopen("https://coub.com/", context=SSLCONTEXT)
    except urllib.error.URLError as e:
        if isinstance(e.reason, SSLCertVerificationError):
            msg.err("Certificate verification failed! Please update your CA certificates.",
                color=msg.ERROR)
        else:
            msg.err("Unable to connect to coub.com! Please check your connection.",
                color=msg.ERROR)
        sys.exit(ERROR_CONN)


def resolve_paths():
    """Change into (and create) the destination directory."""
    if not os.path.exists(opts.path):
        os.makedirs(opts.path)
    os.chdir(opts.path)


def remove_container_dupes(containers):
    """Remove duplicate containers to avoid unnecessary parsing."""
    no_dupes = []
    # Brute-force sorting
    for c in containers:
        unique = True
        for u in no_dupes:
            if (c.type, c.id, c.sort) == (u.type, u.id, u.sort):
                unique = False
        if unique or c.type == "random":
            no_dupes.append(c)

    return no_dupes


def parse_input(sources):
    """Handle the parsing process of all provided input sources."""
    directs = [s for s in sources if isinstance(s, str)]
    containers = [s for s in sources if not isinstance(s, str)]
    containers = remove_container_dupes(containers)

    if opts.max_coubs:
        parsed = directs[:opts.max_coubs]
    else:
        parsed = directs

    if parsed:
        msg.msg("\nReading command line:")
        msg.msg(f"  {len(parsed)} link{'s' if len(parsed) != 1 else ''} found")

    # And now all containers
    for c in containers:
        if opts.max_coubs:
            rest = opts.max_coubs - len(parsed)
            if not rest:
                break
        else:
            rest = None

        if isinstance(c, container.Channel):
            c.set_recoubs(opts.recoubs)
        if not isinstance(c, container.LinkList):
            c.prepare(rest)

        if not c.valid:
            msg.err("\n", c.error, color=msg.WARNING, sep="")
            continue

        if isinstance(c, container.LinkList):
            msg.msg(f"\nReading input list ({c.id}):")
        else:
            msg.msg(f"\nDownloading {c.type} info",
                f": {c.id}"*bool(c.id),
                f" (sorted by '{c.sort}')"*bool(c.sort), sep="")
            msg.msg(f"  {c.max_pages} out of {c.pages} pages")

        level = 0
        while opts.retries < 0 or level <= opts.retries:
            try:
                if isinstance(c, container.LinkList):
                    parsed.extend(asyncio.run(c.process(rest)))
                else:
                    parsed.extend(asyncio.run(c.process(opts.connections, rest)))
                break   # Exit loop on successful completion
            except (aiohttp.ClientConnectionError, aiohttp.ClientPayloadError):
                check_connection()
                level += 1
                msg.err(f"  Retrying... ({level} of "
                    f"{opts.retries if opts.retries > 0 else 'Inf'} attempts)",
                    color=msg.WARNING)

        if isinstance(c, container.LinkList):
            msg.msg(f"  {c.length} link{'s' if c.length != 1 else ''} found")

        if level > opts.retries >= 0:
            msg.err(f"  Can't fetch {c.type} info! Please check your connection.",
                color=msg.ERROR)
            sys.exit(ERROR_CONN)

    if not parsed:
        msg.err("\nNo coub links specified!", color=msg.WARNING)
        sys.exit(ERROR_OPT)

    if opts.max_coubs and len(parsed) >= opts.max_coubs:
        msg.msg(f"\nDownload limit ({opts.max_coubs}) reached!",
            color=msg.WARNING)

    before = len(parsed)
    parsed = list(set(parsed))      # Weed out duplicates
    dupes = before - len(parsed)
    parsed = [i for i in parsed if i not in opts.archive_content]
    archived = before - dupes - len(parsed)
    after = len(parsed)
    if dupes or archived:
        msg.msg(dedent(f"""
            Results:
              {before} input link{'s' if before != 1 else ''}
              {dupes} duplicate{'s' if dupes != 1 else ''}
              {archived} found in archive file
              {after} final link{'s' if after != 1 else ''}"""))
    else:
        msg.msg(dedent(f"""
            Results:
              {after} link{'s' if after != 1 else ''}"""))

    return parsed


def write_list(ids):
    """Output parsed links to a list and exit."""
    with open(opts.output_list, opts.write_method) as f:
        for i in ids:
            print(f"https://coub.com/view/{i}", file=f)
    msg.msg(f"\nParsed coubs written to '{opts.output_list}'!",
        color=msg.SUCCESS)


def clean_workspace(coubs):
    """Clean workspace by deleteing unfinished coubs."""
    for c in [c for c in coubs if not c.done]:
        c.delete()


async def process(coubs):
    """Process (i.e. download) provided Coub objects."""
    level = 0
    while opts.retries < 0 or opts.retries >= level:
        if level > 0:
            msg.err(f"Retrying... ({level} of "
                f"{opts.retries if opts.retries > 0 else 'Inf'} attempts)",
                color=msg.WARNING)

        try:
            tout = aiohttp.ClientTimeout(total=None)
            conn = aiohttp.TCPConnector(limit=opts.connections, ssl=SSLCONTEXT)
            async with aiohttp.ClientSession(timeout=tout, connector=conn) as s:
                tasks = [c.process(s, opts) for c in coubs]
                await asyncio.gather(*tasks)
            return
        except aiohttp.ClientError as e:
            if isinstance(e, aiohttp.ClientConnectionError):
                msg.err("\nLost connection to coub.com!", color=msg.ERROR)
            elif isinstance(e, aiohttp.ClientPayloadError):
                msg.err("\nReceived malformed data!", color=msg.ERROR)
            else:
                msg.err(f"\nMisc. aiohttp.Clienterror ('{e}')!", color=msg.ERROR)
            check_connection()
            # Reduce the list of coubs to only those yet to finish
            coubs = [c for c in coubs if not c.done]
            level += 1

    msg.err("Ran out of connection retries! Please check your connection.",
        color=msg.ERROR)
    clean_workspace(coubs)
    sys.exit(ERROR_CONN)

# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~
# Main Function
# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~

def main():
    """Download all requested coubs."""
    try:
        check_prereq()
        resolve_paths()
        check_connection()

        msg.msg("\n### Parse Input ###")
        ids = parse_input(opts.input)

        if ids:
            if opts.output_list:
                write_list(ids)
                sys.exit(0)
            download.total = len(ids)
            coubs = [download.Coub(i) for i in ids]

            msg.msg("\n### Download Coubs ###\n")
            try:
                asyncio.run(process(coubs), debug=False)
            finally:
                clean_workspace(coubs)
        else:
            msg.msg("\nAll coubs present in archive file!", color=msg.WARNING)

        msg.msg("\n### Finished ###\n")

        # Indicate failure if not all input coubs exist after execution
        if download.done < download.count:
            sys.exit(ERROR_DOWN)
    except KeyboardInterrupt:
        msg.err("\nUser Interrupt!", color=msg.WARNING)
        sys.exit(ERROR_INT)


# Execute main function
if __name__ == '__main__':
    try:
        opts = parse_cli()
    except ConfigError as e:
        msg.err(e, color=msg.ERROR)
        sys.exit(ERROR_OPT)

    msg.set_verbosity(opts.verbosity)

    main()
