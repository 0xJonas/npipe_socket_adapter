#!/usr/bin/python3
# This script is distributed under the MIT license.
# See the full license text at the end of the file.

import argparse
import asyncio
import logging
import signal
import subprocess

CONNECTION_SENTINEL = b"Connected!\r\n"

SCRIPT_SERVER = """\
$pipe = New-Object "System.IO.Pipes.NamedPipeServerStream" -ArgumentList @(
    "{pipe_name}",
    [System.IO.Pipes.PipeDirection]::InOut,
    [System.IO.Pipes.NamedPipeServerStream]::MaxAllowedServerInstances,
    [System.IO.Pipes.PipeTransmissionMode]::Byte,
    [System.IO.Pipes.PipeOptions]::Asynchronous
);
$pipe.WaitForConnection()
Write-Output "Connected!"
[System.threading.Tasks.Task]::WaitAny(@(
    [System.Console]::OpenStandardInput().CopyToAsync($pipe),
    $pipe.CopyToAsync([System.Console]::OpenStandardOutput())
))
$pipe.Close()
"""

SCRIPT_CLIENT = """\
$pipe = New-Object "System.IO.Pipes.NamedPipeClientStream" -ArgumentList @(
    ".",
    "{pipe_name}",
    [System.IO.Pipes.PipeDirection]::InOut,
    [System.IO.Pipes.PipeOptions]::Asynchronous
)
$pipe.Connect()
[System.threading.Tasks.Task]::WaitAny(@(
    [System.Console]::OpenStandardInput().CopyToAsync($pipe),
    $pipe.CopyToAsync([System.Console]::OpenStandardOutput())
))
$pipe.Close()
"""


logger = logging.getLogger("npipe-socket-adapter")
connection_id = 0


async def copy_to_async(reader, writer):
    wait_closed_task = asyncio.create_task(writer.wait_closed())
    while True:
        read_task = asyncio.create_task(reader.read(1024))
        done, _ = await asyncio.wait(
            [read_task, wait_closed_task], return_when=asyncio.FIRST_COMPLETED
        )

        if wait_closed_task in done:
            # Writer was closed
            read_task.cancel()
            break
        elif read_task in done:
            data = read_task.result()
            if data:
                writer.write(data)
                await writer.drain()
            else:
                # reader.read() returned an empty bytes object, so the reader was closed.
                break


def _setup_stop_task():
    event = asyncio.Event()
    asyncio.get_running_loop().add_signal_handler(signal.SIGINT, event.set)
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, event.set)
    return asyncio.create_task(event.wait())


async def serve_named_pipe(pipe_name, powershell_exe, callback):
    async def serve_single(proc):
        global connection_id
        nonlocal pipe_free
        cid = connection_id
        connection_id += 1
        logger.info("New connection: %d.", cid)

        callback_task = asyncio.create_task(callback(proc.stdout, proc.stdin))
        done, _ = await asyncio.wait(
            [callback_task, should_terminate], return_when=asyncio.FIRST_COMPLETED
        )
        if should_terminate in done:
            callback_task.cancel()
            proc.terminate()
        await proc.wait()
        logger.info("Connection closed: %d.", cid)
        if pipe_free is not None:
            pipe_free.set()

    logger.info("Serving named pipe %s.", pipe_name)

    should_terminate = _setup_stop_task()

    # Event to signal that the pipe is definitely free.
    # Used to not unnecessarily try connecting to the pipe in a loop.
    pipe_free = None
    while True:
        if pipe_free is not None:
            await pipe_free
            pipe_free = None

        proc = await asyncio.create_subprocess_exec(
            powershell_exe,
            "-NonInteractive",
            "-NoProfile",
            "-Command",
            SCRIPT_SERVER.format(pipe_name=pipe_name),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            # Put each subprocess into a new process group.
            # This is used to streamline the code for graceful shutdown.
            # Ctrl+C normally sends SIGINT to the entire process group, which
            # would also stop the subprocesses, while `kill <pid>` sends SIGTERM
            # only to the Python process. Putting each subprocess into its own
            # group means that the signals send by Ctrl+C and `kill <pid>` are both
            # only received by the Python process, and the Python process is in charge
            # of stopping its subprocesses.
            start_new_session=True,
        )
        wait_for_connection_task = asyncio.create_task(
            proc.stdout.readexactly(len(CONNECTION_SENTINEL))
        )

        done, _ = await asyncio.wait(
            [wait_for_connection_task, should_terminate],
            return_when=asyncio.FIRST_COMPLETED,
        )
        if should_terminate in done:
            logger.info("Interrupt received.")
            wait_for_connection_task.cancel()
            proc.terminate()
            await proc.wait()
            break

        if wait_for_connection_task.result() == CONNECTION_SENTINEL:
            asyncio.create_task(serve_single(proc))
        else:
            logger.error("Unable to open named pipe server stream.")
            proc.terminate()
            await proc.terminate()
            pipe_free = asyncio.Event()


async def connect_to_named_pipe(pipe_name, powershell_exe, reader, writer):
    proc = await asyncio.create_subprocess_exec(
        powershell_exe,
        "-NonInteractive",
        "-NoProfile",
        "-Command",
        SCRIPT_CLIENT.format(pipe_name=pipe_name),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        start_new_session=True,
    )
    logger.info("Connected to named pipe %s.", pipe_name)
    await asyncio.gather(
        copy_to_async(reader, proc.stdin), copy_to_async(proc.stdout, writer)
    )
    await proc.wait()


async def serve_unix_socket(socket_name, callback):
    async def logging_callback(reader, writer):
        global connection_id
        try:
            cid = connection_id
            connection_id += 1
            logger.info("New connection: %d.", cid)
            await callback(reader, writer)
        finally:
            logger.info("Connection closed: %d.", cid)

    should_terminate = _setup_stop_task()
    server = await asyncio.start_unix_server(logging_callback, socket_name)
    logger.info("Serving UNIX socket %s", socket_name)
    async with server:
        server_task = asyncio.create_task(server.serve_forever())
        done, _ = await asyncio.wait(
            [server_task, should_terminate],
            return_when=asyncio.FIRST_COMPLETED,
        )
        if should_terminate in done:
            logger.info("Interrupt received.")
            server_task.cancel()


async def connect_to_unix_socket(socket_name, reader, writer):
    (socket_reader, socket_writer) = await asyncio.open_unix_connection(socket_name)
    logger.info("Connected to UNIX domain socket %s.", socket_name)
    await asyncio.gather(
        copy_to_async(reader, socket_writer), copy_to_async(socket_reader, writer)
    )
    socket_reader.feed_eof()
    socket_writer.close()


def main():
    parser = argparse.ArgumentParser(
        description="Adapter to convert between Windows named pipes and UNIX domain sockets, in the context of WSL 2."
    )
    parser.add_argument(
        "direction",
        help="""\
Whether to expose an existing Windows named pipe as a UNIX domain socket (npipe-to-socket),
or to expose an existig UNIX domain socket as a Windows named pipe (socket-to-npipe)""",
        choices=["npipe-to-socket", "socket-to-npipe"],
    )
    parser.add_argument(
        "--npipe",
        "-n",
        help=r"Name of the named pipe. The name must NOT include the leading '\\.\pipe\'",
        required=True,
    )
    parser.add_argument(
        "--socket", "-s", help="Name of the UNIX domain socket.", required=True
    )
    parser.add_argument(
        "-p",
        "--powershell",
        help="""\
Location of the Powershell executable to use.
Powershell is used to convert a connection to a named pipe
to standard IO streams.""",
        default="/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe",
    )
    parser.add_argument(
        "--verbose", "-v", help="Enable verbose output.", action="store_true"
    )

    args = parser.parse_args()

    if args.verbose:
        logging.basicConfig(
            format="%(asctime)s [%(levelname)s] %(message)s",
            level=logging.INFO,
        )

    if args.direction == "npipe-to-socket":
        asyncio.run(
            serve_unix_socket(
                args.socket,
                lambda r, w: connect_to_named_pipe(args.npipe, args.powershell, r, w),
            )
        )
    elif args.direction == "socket-to-npipe":
        asyncio.run(
            serve_named_pipe(
                args.npipe,
                args.powershell,
                lambda r, w: connect_to_unix_socket(args.socket, r, w),
            )
        )


if __name__ == "__main__":
    main()


# MIT License
#
# Copyright (c) 2024 Jonas Rinke
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the “Software”), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED “AS IS”, WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
