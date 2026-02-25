# .flaskenv - Flask CLI Environment Settings
# This file is automatically loaded by python-dotenv when running `flask` commands.
# It is version-controlled and should NOT contain secrets or sensitive values.
# Values in .env will override values in this file.
# For production, these settings are overridden by Gunicorn configuration.

# WSGI application entry point
FLASK_APP=wsgi:app

# Default to development environment
FLASK_ENV=development

# Enable debug mode and auto-reloader for development
FLASK_DEBUG=1

# Listen on all interfaces (required for Docker containers)
FLASK_RUN_HOST=0.0.0.0

# Default development server port
FLASK_RUN_PORT=5000
