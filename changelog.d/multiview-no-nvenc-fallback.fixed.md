Multi-View falls back to software encoding when the installed ffmpeg was
built without NVENC or Quick Sync (it used to fail with "Option not found"
and never start).
