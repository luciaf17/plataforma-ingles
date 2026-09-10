web: python manage.py migrate --noinput && python manage.py bootstrap && gunicorn config.wsgi --bind 0.0.0.0:$PORT --workers 2 --timeout 120
