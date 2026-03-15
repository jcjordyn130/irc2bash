#!/usr/bin/env python3
import os
import sys
import socket
import signal
import threading
import subprocess
import re
import time
import queue
import config
import ssl as ssllib

class Server():
    # Order of functions in Server():
    # - Public API (non underscored functions and __init__)
    # - Thread targets
    # - Internal helper functions (message parsing, message handling, etc)
    # - Bot commands (pid, die, etc)

    # Setup the IRC related things such as user details and prefixes
    # This function also sets up the threading events and queues
    def __init__(self, realname, nickname, channels, command_prefix = "$!", bot_prefix = "$$", opper_nicknames = [], message_queue_max_size = 512):
        print(f"[SERVER/MAINTHREAD] Using Server() on Python3 PID {os.getpid()}")
        self.realname = realname
        self.nickname = nickname
        self.channels = channels
        self.opper_nicknames = opper_nicknames

        # Prefix for commands to execute
        self.command_prefix = command_prefix

        # Prefix for internal commands
        self.bot_prefix = bot_prefix

        # Used to signal threads to shutdown
        self._going_down = threading.Event()

        # Used to signal command thread to kill the children process
        self._kill_cmd = threading.Event()

        # Queue used for IRC server send() calls
        self._send_q = queue.Queue(maxsize = message_queue_max_size)

        # Rate limiting variables
        # Used so it can be quired by IRC
        self._msg_time = 0
        self._msg_count = 0

        print(f"[SERVER/MAINTHREAD] Using nickname {nickname}!")

    # This function handles the socket bring up
    # Sets socket paramaters, and starts the send/recv threads
    def connect(self, ip, port, ssl = False, sock_timeout = 60, sock_sendbuf = 512, sock_recvbuf = 512):
        print(f"[SERVER/MAINTHREAD] Connecting to {ip}:{port}")

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

        if ssl:
            print(f"[SERVER/MAINTHREAD] Using SSL for connection!")
            context = ssllib.create_default_context()
            self.sock = context.wrap_socket(sock, server_hostname = ip)
        else:
            print(f"[SERVER/MAINTHREAD] Using plain text for connection!")
            self.sock = sock

        print(f"[SERVER/MAINTHREAD] Using socket timeout of {sock_timeout} seconds!")
        self.sock.settimeout(sock_sendbuf)

        print(f"[SERVER/MAINTHREAD] Using socket send/recv buffer of {sock_sendbuf}/{sock_recvbuf}!")
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, sock_sendbuf)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, sock_recvbuf)

        print("[SERVER/MAINTHREAD] Starting send thread!")
        self._send_thread = threading.Thread(target = self._send_loop, args = (ip,))
        self._send_thread.start()

        print("[SERVER/MAINTHREAD] Starting recieve thread!")
        self._recv_thread = threading.Thread(target = self._recv_loop, args = (ip, port))
        self._recv_thread.start()

    # Gracefully shuts the server down
    def die(self, msg = "Python3 Server() outta here!"):
        # NOOP if we're in the process of going down
        if self._going_down.is_set():
            return

        print("[SERVER] Sending PART and QUIT")
        # Part all of our channels with a cool message, then quit
        for channel in self.channels:
            self.sock.send(f"PART {channel} : {msg}\r\n".encode())

        self.sock.send(f"QUIT : {msg}\r\n".encode())

        print("[SERVER] Signaling threads to quit!")
        # Signal threads to quit
        self._going_down.set()

        print("[SERVER] Signaling main thread to quit!")
        os.kill(os.getpid(), signal.SIGINT)

    # Sends a message to a target (user/channel)
    # The bot does NOT have to be a channel to send a message, but most channels
    # disallow messages from non-JOIN'ed users.
    def privmsg(self, target, message, bypass_q = False):
        # Check for send thread
        try:
            self._send_thread
        except NameError:
            raise RuntimeWarning("connect() must be called before this command is used!")

        # format message
        message = f"PRIVMSG {target} :{message}\r\n".encode()

        if bypass_q:
            self.sock.send(message)
        else:
            self._send_q.put(message)

    # Function used to send messages from the queue
    # Rate limiting is also implemented here
    def _send_loop(self, ip):
        while not self._going_down.is_set():
            self._msg_time = (0.15 * self._msg_count)**2
            print(f"[SENDTHREAD] Sleeping for {self._msg_time} seconds on message count {self._msg_count}")
            time.sleep(self._msg_time)
            if self._msg_count == 10:
                print("[SENDTHREAD] Sending ping on 10th message!")
                self.sock.send(f"PING :{ip}\r\n".encode())
                self._msg_count = 0

            # Grab message and send it
            try:
                msg = self._send_q.get(timeout = 60)

                # Only increment the message count if we actually get a message,
                # the server will PING us when it needs to.
                self._msg_count+=1
            except queue.Empty:
                # So the thread signaler works
                continue

            print(f"[SENDTHREAD] Sending message: {msg}")
            try:
                self.sock.send(msg)
            except BrokenPipeError:
                # Can't use die because we have no connection... just quit I suppose
                print("[SENDTHREAD] Quitting due to BrokenPipeError!")
                break

        print("[SENDTHREAD] Quitting due to thread condition!")

    # This is the function used to process messages from the server
    # This is where we actually connect to the IRC server
    def _recv_loop(self, ip, port):
        # Connect to server
        self.sock.connect((ip, port))

        # Send user reg        
        self._send_userreg()

        # Grab receieve buffer size
        bufsize = self.sock.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)

        # Enter main loop
        while not self._going_down.is_set():
            # Grab IRC data and decode text
            try:
                data = self.sock.recv(bufsize)
                data = data.decode("utf-8")
            except UnicodeDecodeError:
                print("[RECVTHREAD] Error decoding message as utf-8!")
                print("[RECVTHREAD] Message: {data}")
                continue
            except socket.timeout:
                # So threading Events work
                continue

            # Handle IRC messages
            ircmsgs = data.split("\r\n")
            for msg in ircmsgs:
                parsed_msg = self._parse_message(msg)

                # Ignore blank messages
                if parsed_msg:
                    self._handle_message(parsed_msg)

        # Bye bye main loop
        if self._going_down.is_set():
            print("[RECVTHREAD] Quitting due to thread condition!")

    # This is where we actually run the RCE commands and pipe the output
    # back to IRC.
    #
    # No temp files here
    def _handle_command(self, cmd, target_channel):
        print(f"[CMDTHREAD] Running CMD {cmd}!")
        proc = subprocess.Popen(cmd, shell = True, stdout = subprocess.PIPE, stderr = subprocess.STDOUT, stdin = subprocess.DEVNULL,
        start_new_session=True, text=False)

        # Calculate maximum message size            
        # 512 bytes is max IRC message with <IRCv3 and no cap neg
        msg_without_data = b"PRIVMSG " + target_channel.encode() + b" :" + b"\r\n"
        max_data_len = 512 - len(msg_without_data)
        print(f"[CMDTHREAD] Reading from Popen pipe with len = {max_data_len}!")

        while True:
            # Check for quitting flag
            if self._going_down.is_set():
                print("[CMDTHREAD] Quitting due to thread condition!")
                proc.kill()
                break

            # Check for kill children flag
            if self._kill_cmd.is_set():
                print(f"[CMDTHREAD] Quitting due to bot command!")
                proc.kill()
                self._kill_cmd.clear()
                break

            # Read a line up to max_data_len
            # Replace invalid chars with escape sequences
            line = proc.stdout.readline(max_data_len)
            line = line.decode("utf8", errors = "backslashreplace")

            # Check for empty data
            if not line:
                break

            # Remove carraige return to avoid confusing IRC server
            line = line.replace("\r", "")

            # Strip rouge newlines from data
            line = line.replace("\r\n", "")
            line = line.replace("\n", "")

            self.privmsg(target_channel, line)

            # Break if our child exits for any reason
            if proc.poll():
                break

    # This function sends the user details to the IRC server
    # This isn't in the recv thread as we need to run it twice
    # if the nick is in use.
    def _send_userreg(self):       
        self._send_q.put(f"USER {self.realname} * * :{self.nickname}\r\n".encode())
        self._send_q.put(f"NICK {self.nickname}\r\n".encode())

        for channel in self.channels:
            print(f"[RECVTHREAD] Joining channel {channel}!")
            self._send_q.put(f"JOIN {channel}\r\n".encode())
  
    # Used to strip ASCII control characters (such as terminal escape codes)
    # from text. Mainly to avoid unknown command errors from the IRC server.
    def _strip_control_chars(self, text):
        control_char_re = r"[\x00-\x1F\x7F]"
        return re.sub(control_char_re, "*", text)

    # This function clears the send queue by fetching all of the items in a loop 
    # before the send thread can grab them.
    def _clear_sendq(self):
        print(f"[ONESHOTTHREAD] Clearing sendq of size {self._send_q.qsize()}")

        try:
            while True:
                self._send_q.get_nowait()
        except queue.Empty:
            print("[ONESHOTTHREAD] Queue cleared!")
            pass

    # This function is used to run a function in a new daemonic thread without waiting.
    # Used for command handlers, mainly clearsendq to speed up
    # queue clearning during heavy server load.
    def _oneshot_thread(self, func, args = []):
        print(f"[SERVER] Starting oneshot thread for {func} with args = {args}")
        thread = threading.Thread(target = func, args = args, daemon = True)
        thread.start()

    # This is where the message parsing actually happens
    def _parse_message(self, msg):
        # Set optional values to None to avoid UnboundLocalError's
        prefix = None
        trailing = None

        # Strip newlines from message
        msg = msg.rstrip("\r\n")

        # Ignore empty messages
        if not msg:
            return

        # Extract message prefix if there is one
        if msg.startswith(":"):
            # Find the first space to split the prefix
            prefix_end = msg.find(" ")
            if prefix_end == -1:
                # Huh?
                return

            # Split out the prefix
            prefix = msg[1:prefix_end]
            msg = msg[prefix_end + 1:]

        # Extract any trailing parameters
        trailing_idx = msg.find(" :")
        if trailing_idx != -1:
            trailing = msg[trailing_idx + 2:]
            msg = msg[:trailing_idx]

        # Extract the command and regular parameters
        parts = msg.split()
        if not parts:
            # Huh?
            return None
        command = parts[0].upper()
        params = parts[1:]

        # Add trailing parameter to params list
        if trailing:
            params.append(trailing)

        # Extract possible user info from prefix
        if prefix:
            message_source = prefix.partition("!")[0]

            # Technically the target is ourselfs, but I change it to be the other user
            # so other parts of the script can easily use it to know where to send output.
            try:
                if params[0] == self.nickname:
                    target_channel = message_source
                else:
                    target_channel = params[0]
            except IndexError:
                # We got an empty command? what
                # ZNC loves to send these
                target_channel = None
                
        else:
            message_source = None
            target_channel = None

        return {
            "prefix": prefix,
            "command": command,
            "params": params,
            "message_source": message_source,
            "target_channel": target_channel
        }

    # This is where we handle messages that need to be
    # Most of them can safely be ignored.
    def _handle_message(self, msg):
        debug_msg = self._strip_control_chars(f"[RECVTHREAD] Got message from server! Message: {msg}")
        print(debug_msg)

        # Handle PINGs from server
        if msg["command"] == "PING":
            self.sock.send(f"PONG {msg['params'][0]}\r\n".encode())
            return 

        # Handle PRIVMSG
        if msg["command"] == "PRIVMSG":
            # Ignore empty PRIVMSGs, ZNC loves to send these.
            if not len(msg["params"]):
                print(f"[RECVTHREAD] Ignoring empty PRIVMSG from server!")
                return

            # Check for command prefix and run it if we got one
            if msg["params"][-1].startswith(self.command_prefix):
                # Strip command prefix and spaces if any from input
                cmd = msg["params"][-1].strip(self.command_prefix)
                cmd = cmd.strip(" ")

                # Ignore blank commands
                if not cmd:
                    return

                # Run CMDTHREAD
                threading.Thread(target = self._handle_command, args = (cmd, msg["target_channel"], )).start()
            elif msg["params"][-1].startswith(self.bot_prefix):
                # Get rid of the bot prefix
                command = msg["params"][-1].strip(self.bot_prefix)

                # Get rid of any leading spaces
                command = command.strip(" ")

                # Ignore blank commands
                if not command:
                    return

                # Lookup command
                try:
                    command_func = getattr(self, f"_cmd_{command}")
                except AttributeError:
                    # Invalid command
                    print(f"[RECVTHREAD] _cmd_{command} was not found!")
                    return

                # Run command
                print(f"[RECVTHREAD] Running command _cmd_{command}!")
                command_func(msg)

        # Handle nickname already in use by appending the PID
        # and resending user reg
        elif msg["command"] == "433":
            self.nickname = f"{self.nickname}-{os.getpid()}"

            print(f"[RECVTHREAD] Nickname already in use! Using: {self.nickname}")
            self._send_userreg()
        # Allow users to add bots to channel, but only if it's me
        elif msg["command"] == "INVITE":
            if msg["message_source"] in self.opper_nicknames:
                print(f"[RECVTHREAD] Joining channel by opper command from {msg['message_source']}!")
                self._send_q.put(f"JOIN {msg['params'][-1]}\r\n".encode())
                self.channels.append(msg["params"][-1])
        elif msg["command"] == "NICK":
            # Somebody changed their nick, or the server forced us to change ours.
            if msg["message_source"] == self.nickname:
                print(f"[RECVTHREAD] Server forced nickname change to {msg['params'][0]}!")
                self.nickname = msg["params"][0]

    # These are where bot commands are implemented
    # The format is _cmd_NAMEOFCOMMAND and it gets the class instance and the triggering message as parameters.
    # The functions are looked up dynamically in _handle_message and executed.
    def _cmd_help(self, msg):
        for line in [f"Current bot prefix: {self.bot_prefix}", f"Current RCE prefix: {self.command_prefix}"]:
            self.privmsg(msg["target_channel"], line)

    def _cmd_sendqlen(self, msg):
        self.privmsg(msg["target_channel"], f"Send Queue Size: {self._send_q.qsize()}")

    def _cmd_pid(self, msg):
        self.privmsg(msg["target_channel"], f"Bot PID: {os.getpid()}")

    def _cmd_die(self, msg):
        # Tear down server
        self.die()

    def _cmd_floodstats(self, msg):
        self.privmsg(msg["target_channel"], f"Sleep Time: {self._msg_time}")
        self.privmsg(msg["target_channel"], f"Message Count: {self._msg_count}")

    def _cmd_clearsendq(self, msg):
        self.privmsg(msg["target_channel"], f"Clearing sendq of size {self._send_q.qsize()}", bypass_q = True)
        self._oneshot_thread(self._clear_sendq)

    def _cmd_killcmd(self, msg):
        print("[RECVTHREAD] Signaling CMDTHREAD(s) to kill their children!")
        self._kill_cmd.set()

        # Also clear the sendq as this command will typically be used when doing
        # something such as catting /dev/urandom
        self._oneshot_thread(self._clear_sendq)

if __name__ == "__main__":
    serv = Server(**config.user, **config.bot)
    serv.connect(**config.server)

    # Allow for the user to send raw IRC messages
    # These bypass the send queue
    while True:
        try:
            if sys.stdin.isatty():
                rawcmd = input()
                serv.sock.send(f"{rawcmd}\r\n".encode())
            else:
                serv._going_down.wait()
        except KeyboardInterrupt:
            print("[MAINTHREAD] Interrupt receieved... server going down!")
            print("[MAINTHREAD] Could take up to 60 seconds for socket timeout!")
            serv.die()
            raise SystemExit(0)