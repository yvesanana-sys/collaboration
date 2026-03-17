web: gunicorn --bind 0.0.0.0:$PORT --workers 1 --threads 2 bot_with_proxy:app & python -c "import time; [time.sleep(1) for _ in iter(int,1)]"
