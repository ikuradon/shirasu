"""Pure health evaluation shared by the receiver CLI and metrics exporter."""


def healthy(snapshot, now):
    state = snapshot['status']
    heartbeat = state.get('heartbeat', 0)
    pending_age = snapshot.get('oldest_pending_age_seconds')
    return bool(state.get('running') and state.get('gpsd_connected')
                and state.get('relay_connected') and 0 <= now - heartbeat < 90
                and 0 <= now - state.get('last_received', 0) < 90
                and not state.get('last_delivery_error')
                and (pending_age is None or pending_age < 90))
