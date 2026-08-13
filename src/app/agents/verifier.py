"""Verifier Agent: runs tests/health checks against a Recovery Agent fix.
On failure, sends new evidence back to the Decision Agent for another
diagnosis cycle, bounded by MAX_RECOVERY_ATTEMPTS. Not yet implemented.
"""
