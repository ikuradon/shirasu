import re
import socket
import azarashi
from pprint import pprint
import sys
import asyncio
from nostr_sdk import *

import config

class Nostr:
  async def init(self, SK, RELAY_URLS):
    keys = Keys.parse(SK)
    signer = NostrSigner.keys(keys)
    self.client = Client(signer)
    for relay_url in RELAY_URLS.split(','):
      await self.client.add_relay(relay_url)
    await self.client.connect()

  async def post(self, message):
    builder = EventBuilder.text_note(message)
    await self.client.send_event_builder(builder)

async def a_shirasu(report, nostr):
  ppp(report)
  message = \
    f"{report}" "\n" \
    "---" "\n" \
    f"NMEA: {report.nmea}"
  await nostr.post(message)

def shirasu(report, loop, nostr):
  ignore_pattern = re.compile(r'QzssDcxNullMsg|QzssDcxUnknown')
  if not ignore_pattern.match(report.__class__.__name__):
    loop.run_until_complete(a_shirasu(report, nostr))


def ppp(report):
  print("#----------")
  print(report)
  print("-----------")
  pprint(report.get_params(), sort_dicts=False)
  print("#----------")

def main(loop):
  nostr = Nostr()
  loop.run_until_complete(nostr.init(config.SECRET_KEY, config.RELAY_URLS))

  s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
  s.connect((config.GPSD_HOST, config.GPSD_PORT))
  s.send(b'?WATCH={"enable":true,"raw":2}')
  stream = s.makefile()

  loop.run_until_complete(nostr.post("booting..."))

  try:
    azarashi.decode_stream(stream, msg_type="ublox", callback=shirasu, callback_args=[loop, nostr], unique=True, ignore_dcx=False)
  except azarashi.QzssDcrDecoderException as e:
    print(f'# [{type(e).__name__}] {e}', file=sys.stderr)
  except azarashi.QzssDcrDecoderNotImplementedError as e:
    print(f'# [{type(e).__name__}] {e}', file=sys.stderr)
  except EOFError as e:
    print(f'{e}', file=sys.stderr)
    return 0
  except Exception as e:
    print(f'# [{type(e).__name__}] {e}', file=sys.stderr)
    return 1


if __name__ == '__main__':
  loop = asyncio.get_event_loop()
  try:
    main(loop)

  except(KeyboardInterrupt, SystemExit):
    print("\nShutdown...")
