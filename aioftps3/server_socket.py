from asyncio import (
    CancelledError,
    Future,
    current_task,
)
from socket import (
    AF_INET,
    IPPROTO_TCP,
    SHUT_RDWR,
    SO_REUSEADDR,
    SOCK_STREAM,
    SOL_SOCKET,
    socket,
)
from ssl import (
    HAS_TLSv1_1,
    HAS_TLSv1_2,
    HAS_TLSv1_3,
    SSLWantReadError,
    SSLWantWriteError,
)
from uuid import (
    uuid4
)

from aioftps3.server_logger import (
    get_child_logger,
    logged,
)

# The main reason for these low-level functions is to beter be able to upgrade
# a non-encrypted socket to an encrypted one half way though a connection,
# which is needed for explicit FTPS. Using start_server + start_tls is
# possible, but very awkward and uses private members of Python standard
# library objects https://bugs.python.org/issue34975
#
# More specifically, the functions here accept a function to get a socket
# object: this allows it to be replaced later. In our case, exactly when the
# connection is upgraded to TLS. This allows client code like
#
# async for line in recv_lines(..., get_sock, ...):
#   ...
#
# to be run before the TLS upgrade, but continue to iterate after the TLS
# upgrade, returning decrypted data. AFAIK this sort of code is not possible
# with the Python asyncio framework of protocol, transports, stream readers,
# and stream writers

LINE_ENDING = b'\r\n'
MAX_LINE_LENGTH = 1024


class SocketClosed(Exception):
    pass


async def server(logger, loop, get_ssl_context, port, on_listening, client_handler, on_cancel):

    with logged(logger, 'Starting server on %s', [port]):
        sock = socket(family=AF_INET, type=SOCK_STREAM, proto=IPPROTO_TCP)
        sock.setsockopt(SOL_SOCKET, SO_REUSEADDR, 1)
        sock.setblocking(False)
        sock.bind(('', port))
        sock.listen(IPPROTO_TCP)

    tasks = set()

    async def client_task(client_logger, client_sock):

        try:
            with logged(client_logger, 'Client handler', []):
                try:
                    await client_handler(client_logger, loop, get_ssl_context, client_sock)
                except SocketClosed:
                    client_logger.debug('Socket closed')
        except CancelledError:
            raise
        except BaseException:
            client_logger.exception('Exception in socket client handler')
        finally:
            client_sock.close()
            tasks.remove(current_task())

    try:
        with logged(logger, 'Listening for clients', []):
            while True:
                client_sock, address = await sock_accept(loop, sock, on_listening)
                unique_id = str(uuid4())[:8]
                client_logger = get_child_logger(logger, unique_id)
                client_logger.debug('Connection from %s', address)
                tasks.add(loop.create_task(client_task(client_logger, client_sock)))
    except CancelledError:
        await on_cancel(tasks)
        raise
    except BaseException:
        logger.exception('Exception listening for socket')
    finally:
        try:
            sock.shutdown(SHUT_RDWR)
        except OSError:
            pass
        sock.close()


async def sock_accept(loop, sock, on_listening):
    fileno = sock.fileno()
    done = Future()

    def accept_without_reader():
        try:
            conn, address = sock.accept()
            conn.setblocking(False)
            on_listening(True)
        except BlockingIOError:
            loop.add_reader(fileno, accept_with_reader)
            on_listening(True)
        except BaseException as exception:
            on_listening(False)
            if not done.cancelled():
                done.set_exception(exception)
        else:
            done.set_result((conn, address))

    def accept_with_reader():
        try:
            conn, address = sock.accept()
            conn.setblocking(False)
        except BlockingIOError:
            pass
        except BaseException as exception:
            loop.remove_reader(fileno)
            if not done.cancelled():
                done.set_exception(exception)
        else:
            loop.remove_reader(fileno)
            done.set_result((conn, address))

    accept_without_reader()

    try:
        return await done
    except CancelledError:
        loop.remove_reader(fileno)
        raise


async def shutdown_socket(loop, sock):
    incoming = memoryview(bytearray(128))

    try:
        sock.shutdown(SHUT_RDWR)
        while True:
            await recv(loop, lambda: sock, 128, incoming)
    except BaseException:
        pass


def ssl_get_socket(logger, get_ssl_context, sock):
    logger.debug('HAS_TLSv1_1: %s', HAS_TLSv1_1)
    logger.debug('HAS_TLSv1_2: %s', HAS_TLSv1_2)
    logger.debug('HAS_TLSv1_3: %s', HAS_TLSv1_3)
    return get_ssl_context(sock).wrap_socket(sock, server_side=True, do_handshake_on_connect=False)


async def ssl_complete_handshake(loop, ssl_sock):
    fileno = ssl_sock.fileno()
    done = Future()

    def handshake():
        try:
            ssl_sock.do_handshake()
            if not done.cancelled():
                done.set_result(None)
        except SSLWantReadError:
            loop.add_reader(fileno, handshake_with_reader)
        except SSLWantWriteError:
            loop.add_writer(fileno, handshake_with_writer)
        except BaseException as exception:
            if not done.cancelled():
                done.set_exception(exception)

    def handshake_with_reader():
        loop.remove_reader(fileno)
        handshake()

    def handshake_with_writer():
        loop.remove_writer(fileno)
        handshake()

    handshake()

    try:
        return await done
    except CancelledError:
        loop.remove_reader(fileno)
        loop.remove_writer(fileno)
        raise

    return done


async def ssl_unwrap_socket(loop, ssl_sock, original_sock):
    fileno = ssl_sock.fileno()
    done = Future()

    def unwrap():
        try:
            sock = ssl_sock.unwrap()
            if not done.cancelled():
                done.set_result(sock)
        except SSLWantReadError:
            loop.add_reader(fileno, unwrap_with_reader)
        except SSLWantWriteError:
            loop.add_writer(fileno, unwrap_with_writer)
        except BaseException:
            if not done.cancelled():
                done.set_result(original_sock)

    def unwrap_with_reader():
        loop.remove_reader(fileno)
        unwrap()

    def unwrap_with_writer():
        loop.remove_writer(fileno)
        unwrap()

    unwrap()

    try:
        return await done
    except CancelledError:
        loop.remove_reader(fileno)
        loop.remove_writer(fileno)
        raise

    return done


async def send_line(loop, get_sock, max_send_size, line):
    await send_all(loop, get_sock, max_send_size, memoryview(line + LINE_ENDING))


async def send_lines(loop, get_sock, max_send_size, lines):
    for line in lines:
        await send_all(loop, get_sock, max_send_size, memoryview(line + LINE_ENDING))


async def send_all(loop, get_sock, max_send_size, buf_memoryview):
    cursor = 0
    while cursor != len(buf_memoryview):
        num_bytes = await send(loop, get_sock, max_send_size, buf_memoryview[cursor:])
        cursor += num_bytes


async def recv_lines(loop, get_sock, max_recv_size):
    received = b''
    incoming = memoryview(bytearray(max_recv_size))

    while True:
        num_bytes = await recv(loop, get_sock, max_recv_size, incoming)
        received = received + bytes(incoming[:num_bytes])
        if len(received) > MAX_LINE_LENGTH:
            raise Exception('Line too long')
        index = received.find(LINE_ENDING)
        if index != -1:
            line, received = received[:index], received[index+len(LINE_ENDING):]
            yield line


async def recv_until_close(loop, get_sock, max_recv_size):
    incoming_buf = bytearray(max_recv_size)
    incoming = memoryview(incoming_buf)

    try:
        while True:
            num_bytes = await recv(loop, get_sock, max_recv_size, incoming)
            yield incoming_buf[:num_bytes]
    except SocketClosed:
        pass


# There is some duplication here. However, this is done to optimize both the
# cases of a write where a writer didn't have to be added, and when we know a
# writer was added. There are no dynamic checks on if we have added them: we
# know by nature of what function we are in. Similarly for recv.
#
# Reasons for not using loop.sock_recv / loop.sock_send or similar:
# - They don't seem to catch the nonblocking SSL exceptions, and so we would
#   need separate functions for SSL sockets
# - The _do_ have the dynamic checks mentioned


async def send(loop, get_sock, max_send_size, buf_memoryview):
    fileno = get_sock().fileno()
    max_bytes = min(max_send_size, len(buf_memoryview))
    result = Future()

    def write_without_writer():
        try:
            num_bytes = get_sock().send(buf_memoryview[:max_bytes])

        except (SSLWantWriteError, BlockingIOError):
            loop.add_writer(fileno, write_with_writer)

        except BaseException as exception:
            if not result.cancelled():
                result.set_exception(exception)

        else:
            if num_bytes == 0:
                result.set_exception(SocketClosed())
            else:
                result.set_result(num_bytes)

    def write_with_writer():
        try:
            num_bytes = get_sock().send(buf_memoryview[:max_bytes])

        except (SSLWantWriteError, BlockingIOError):
            pass

        except BaseException as exception:
            loop.remove_writer(fileno)
            if not result.cancelled():
                result.set_exception(exception)

        else:
            loop.remove_writer(fileno)
            if num_bytes == 0:
                result.set_exception(SocketClosed())
            else:
                result.set_result(num_bytes)

    write_without_writer()

    try:
        return await result
    except CancelledError:
        loop.remove_writer(fileno)
        raise

    return result


async def recv(loop, get_sock, max_recv_size, buf_memoryview):
    fileno = get_sock().fileno()
    max_bytes = min(max_recv_size, len(buf_memoryview))
    result = Future()

    def read_without_reader():
        try:
            num_bytes = get_sock().recv_into(buf_memoryview, max_bytes)

        except (SSLWantReadError, BlockingIOError):
            loop.add_reader(fileno, read_with_reader)

        except BaseException as exception:
            if not result.cancelled():
                result.set_exception(exception)

        else:
            if num_bytes == 0:
                result.set_exception(SocketClosed())
            else:
                result.set_result(num_bytes)

    def read_with_reader():
        try:
            num_bytes = get_sock().recv_into(buf_memoryview, max_bytes)

        except (SSLWantReadError, BlockingIOError):
            pass

        except BaseException as exception:
            loop.remove_reader(fileno)
            if not result.cancelled():
                result.set_exception(exception)

        else:
            loop.remove_reader(fileno)
            if num_bytes == 0:
                result.set_exception(SocketClosed())
            else:
                result.set_result(num_bytes)

    read_without_reader()

    try:
        return await result
    except CancelledError:
        loop.remove_reader(fileno)
        raise
