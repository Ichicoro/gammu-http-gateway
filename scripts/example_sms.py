#!/usr/bin/env python3
"""
Example SMS webhook receiver.

The daemon POSTs JSON to ON_SMS_WEBHOOK:
  {"number": "+15551234567", "text": "Hello!"}

Run this alongside the daemon and set:
  ON_SMS_WEBHOOK=http://localhost:9000/sms

Usage:
  python scripts/example_sms.py
"""

from aiohttp import web
import logging
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", stream=sys.stdout)
log = logging.getLogger("sms_webhook")


async def handle_sms(req: web.Request) -> web.Response:
    data = await req.json()
    number = data.get("number", "unknown")
    text = data.get("text", "")

    log.info("SMS from %s: %s", number, text)

    # Put your logic here — forward to Slack, trigger automation, etc.

    return web.Response(status=200)


app = web.Application()
app.router.add_post("/sms", handle_sms)

if __name__ == "__main__":
    web.run_app(app, host="0.0.0.0", port=9000)
