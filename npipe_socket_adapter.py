#!/usr/bin/python3

import argparse
import asyncio
import logging
import subprocess

POWERSHELL_EXE = "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
CONNECTION_SENTINEL = b"Connected!\r\n"

SCRIPT_SERVER = """\
$pipe = New-Object "System.IO.Pipes.NamedPipeServerStream" -ArgumentList @(
    "{pipe_name}",
    [System.IO.Pipes.PipeDirection]::InOut,
    [System.IO.Pipes.NamedPipeServerStream]::MaxAllowedServerInstances,
    [System.IO.Pipes.PipeTransmissionMode]::Byte,
    [System.IO.Pipes.PipeOptions]::Asynchronous
);
$pipe.WaitForConnection();
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
    try:
        async for d in reader:
            writer.write(d)
            await writer.drain()
    finally:
        writer.close()


async def serve_named_pipe(pipe_name, callback):
    async def serve_single(proc):
        global connection_id
        nonlocal pipe_free
        cid = connection_id
        connection_id += 1
        logger.info("New connection: %d.", cid)
        try:
            await callback(proc.stdout, proc.stdin)
        finally:
            await proc.wait()
            logger.info("Connection closed: %d.", cid)
            if pipe_free is not None:
                pipe_free.set()

    logger.info("Serving named pipe %s.", pipe_name)

    # Event to signal that the pipe is definitely free.
    # Used to not unnecessarily try connecting to the pipe in a loop.
    pipe_free = None
    while True:
        try:
            if pipe_free is not None:
                await pipe_free
                pipe_free = None

            proc = await asyncio.create_subprocess_exec(
                POWERSHELL_EXE,
                "-Command",
                SCRIPT_SERVER.format(pipe_name=pipe_name),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
            )
            output = await proc.stdout.readexactly(len(CONNECTION_SENTINEL))
        except asyncio.CancelledError:
            proc.terminate()
            raise

        if output == CONNECTION_SENTINEL:
            asyncio.create_task(serve_single(proc))
        else:
            logger.error("Unable to open named pipe server stream.")
            proc.terminate()
            pipe_free = asyncio.Event()


async def connect_to_named_pipe(pipe_name, reader, writer):
    proc = await asyncio.create_subprocess_exec(
        POWERSHELL_EXE,
        "-Command",
        SCRIPT_CLIENT.format(pipe_name=pipe_name),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )
    logger.info("Connected to named pipe %s.", pipe_name)
    await asyncio.gather(
        copy_to_async(reader, proc.stdin), copy_to_async(proc.stdout, writer)
    )
    await proc.wait()


async def serve_unix_socket(socket_name, callback):
    def logging_callback(reader, writer):
        global connection_id
        cid = connection_id
        connection_id += 1
        logger.info("New connection: %d.", cid)
        callback(reader, writer)
        logger.info("Connection closed: %d.", cid)

    server = await asyncio.start_unix_server(logging_callback, socket_name)
    logger.info("Serving UNIX socket %s", socket_name)
    async with server:
        await server.serve_forever()


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
Directing in which to forward the data. Must be one of:
- 'npipe-to-socket': Expose an existing named pipe as a UNIX domain socket within WSL 2.
- 'socket-to-npipe': Expose an existing UNIX domain socket as a named pipe within the Windows host..
""",
    )
    parser.add_argument("--npipe", "-n", help="Name of the named pipe.", required=True)
    parser.add_argument(
        "--socket", "-s", help="Name of the UNIX domain socket.", required=True
    )
    parser.add_argument(
        "--verbose", "-v", help="Enable verbose output.", action="store_true"
    )

    args = parser.parse_args()

    if args.verbose:
        logging.basicConfig(level=logging.INFO)

    if args.direction == "npipe-to-socket":
        asyncio.run(
            serve_unix_socket(
                args.socket, lambda r, w: connect_to_named_pipe(args.npipe, r, w)
            )
        )
    elif args.direction == "socket-to-npipe":
        asyncio.run(
            serve_named_pipe(
                args.npipe, lambda r, w: connect_to_unix_socket(args.socket, r, w)
            )
        )


if __name__ == "__main__":
    main()
