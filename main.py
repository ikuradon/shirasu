import socket
import azarashi
from azarashi.reports import dcx
from datetime import timedelta
from pprint import pprint
import sys
import asyncio
from nostr_sdk import ClientBuilder, EventBuilder, Keys, Kind, RelayUrl, SignerAuthenticator

import config

class Nostr:
  def __init__(self):
    self.client = None

  async def init(self, SK, RELAY_URLS):
    self.keys = Keys.parse(SK)
    self.client = ClientBuilder().authenticator(SignerAuthenticator(self.keys)).build()
    for relay_url in RELAY_URLS.split(','):
      await self.client.add_relay(RelayUrl.parse(relay_url.strip()))
    await self.client.connect(and_wait=timedelta(seconds=15))

  async def post(self, message):
    event = EventBuilder(Kind(1), message).finalize(self.keys)
    result = await self.client.send_event(event)
    for relay_url, reason in result.failed.items():
      print(f'# Nostr relay {relay_url}: {reason}', file=sys.stderr)
    if not result.success:
      raise RuntimeError('No Nostr relay accepted the event')
    return result

  async def close(self):
    if self.client is not None:
      await self.client.shutdown()

async def a_shirasu(report, nostr):
  ppp(report)
  message = \
    f"{report}" "\n" \
    "---" "\n" \
    f"NMEA: {report.nmea}"
  await nostr.post(message)

def shirasu(report, loop, nostr):
  if not isinstance(report, (dcx.NullMsg, dcx.Unknown)):
    loop.run_until_complete(a_shirasu(report, nostr))


def ppp(report):
  print("#----------")
  print(report)
  print("-----------")
  pprint(report.get_params(), sort_dicts=False)
  print("#----------")

def main(loop):
  nostr = Nostr()
  try:
    loop.run_until_complete(nostr.init(config.SECRET_KEY, config.RELAY_URLS))
    with socket.create_connection((config.GPSD_HOST, config.GPSD_PORT)) as s:
      s.sendall(b'?WATCH={"enable":true,"raw":2};\n')
      with s.makefile('rb') as stream:
        loop.run_until_complete(nostr.post("booting..."))
        while True:
          try:
            azarashi.decode_stream(
              stream, msg_type="ublox", callback=shirasu,
              callback_args=(loop, nostr), unique=True, ignore_dcx=False,
            )
          except azarashi.AzarashiReadOn as e:
            print(f'# [{type(e).__name__}] {e}', file=sys.stderr)
  except azarashi.AzarashiReopenStream as e:
    print(f'# [{type(e).__name__}] {e}', file=sys.stderr)
    return 1
  except EOFError as e:
    print(f'{e}', file=sys.stderr)
    return 0
  except Exception as e:
    print(f'# [{type(e).__name__}] {e}', file=sys.stderr)
    return 1
  finally:
    loop.run_until_complete(nostr.close())


if __name__ == '__main__':
  try:
    with asyncio.Runner() as runner:
      sys.exit(main(runner.get_loop()))
  except KeyboardInterrupt:
    print("\nShutdown...")
