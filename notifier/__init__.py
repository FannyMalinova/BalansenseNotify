"""BalanSenseNotify -- sign-up notifications for the BalanSense studio.

Separate from the FitSys scheduler on purpose: that program builds the weekly
schedule and writes to the calendar, this one only watches bookings and sends a
message. Nothing here writes to FitSys, and no notification code lives there.

Layering, from pure to risky:
  watch.py        booking counts -> what changed (pure: no clock, no network)
  state.py        the snapshot on disk (event ids and integers only)
  fitsys_link.py  the one seam onto the scheduler's package
  reader.py       read-only calendar reads through fitsys.http_client
  channel.py      which way a notification leaves; the Console fallback
  telegram.py     Telegram (the least trouble to set up)
  mail.py         e-mail over SMTP (the default channel)
  viber.py        Viber, for a bot registered before its Feb 2024 fee
  cli.py          the command line cron runs

Submodules are not imported here, so `import notifier.watch` stays pure and
cheap and needs neither `requests` nor the scheduler on disk.
"""
