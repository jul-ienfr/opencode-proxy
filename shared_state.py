"""
Shared state module for cross-module access.

Avoids circular imports and module loading issues between opencode.py
and dashboard/api.py by providing a single source of truth for
shared objects like VPN manager and free IP pool.
"""

vpn_manager = None
free_ip_pool = None
