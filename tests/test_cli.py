"""Tests for CLI argument parsing."""

import argparse


def test_no_rugcheck_flag_enabled():
    """Test that --no-rugcheck flag is parsed correctly when enabled."""
    parser = argparse.ArgumentParser()
    parser.add_argument('--no-rugcheck', action='store_true')
    parser.add_argument('query', nargs='?')
    
    # Test with --no-rugcheck flag
    args = parser.parse_args(['--no-rugcheck', 'test query'])
    assert args.no_rugcheck is True
    assert args.query == 'test query'


def test_no_rugcheck_flag_default():
    """Test that --no-rugcheck flag defaults to False."""
    parser = argparse.ArgumentParser()
    parser.add_argument('--no-rugcheck', action='store_true')
    parser.add_argument('query', nargs='?')
    
    # Without flag - should default to False
    args = parser.parse_args(['test query'])
    assert args.no_rugcheck is False
    assert args.query == 'test query'
