"""Tests for configuration validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import Settings


