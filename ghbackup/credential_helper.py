#!/usr/bin/env python3
"""Git invokes this executable; credentials are never persisted to a URL/config."""

import os
import sys

if sys.argv[1:] == ["get"]:
    fields = dict(line.rstrip("\n").split("=", 1) for line in sys.stdin if "=" in line)
    if fields.get("protocol") == "https" and fields.get("host") == "github.com":
        print("username=x-access-token")
        print("password=" + os.environ.get("GHBACKUP_TOKEN", ""))
