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
    try:
        data = await req.json()
    except Exception as exc:
        log.error("Bad request body: %s", exc)
        return web.Response(status=400)

    number = data.get("number", "unknown")
    text = data.get("text", "")

    log.info("SMS from %s: %s", number, text)

    try:
        # Put your logic here — forward to Slack, trigger automation, etc.
        pass
    except Exception as exc:
        log.error("Handler failed for SMS from %s: %s", number, exc)
        return web.Response(status=500)

    log.info("SMS from %s handled successfully", number)
    return web.Response(status=200)


app = web.Application()
app.router.add_post("/sms", handle_sms)

if __name__ == "__main__":
    web.run_app(app, host="0.0.0.0", port=9000)
