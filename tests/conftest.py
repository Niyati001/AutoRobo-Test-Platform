"""Pytest configuration and shared fixtures for ARVP test suite."""

import pytest


def pytest_configure(config):
    config.addinivalue_line("markers", "integration: mark as integration test (requires Docker)")
    config.addinivalue_line("markers", "unit: mark as unit test")
