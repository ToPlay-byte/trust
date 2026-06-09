import sys
import os
import django

# Динамически берём путь к папке trustpilot (родитель папки modules)
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ['DJANGO_SETTINGS_MODULE'] = 'parser_app.settings'
django.setup()