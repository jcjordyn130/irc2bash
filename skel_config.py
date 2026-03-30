user = {
	"realname": "setme",
	"nickname": "setme",
	"channels": ["##join", "##these", "##channels"]
}

server = {
	"ip": "irc.serv.net",
	"port": 6667,
	"ssl": False,
	"sock_timeout": 60,
	"sock_sendbuf": 512,
	"sock_recvbuf": 512
}

bot = {
	"command_prefix": "$!",
	"bot_prefix": "$$",
	"opper_nicknames": ["nicknamehere"],

	# 512 messages * 512 bytes maximum per message = 262144 bytes max message queue size
	# Adjust for RAM needs to avoid cat /dev/urandom from causing OOM
	"message_queue_max_size": 512,

	# This is a config option because it can be quite heavy
	"convert_ascii_colors": True
}
