FROM python:3.12.9-alpine

# Set working directory
WORKDIR /app

# Copy app files
ADD . /app/

# Install dependencies
RUN pip install --no-cache-dir -r requirement.txt

# Use ENTRYPOINT to allow Docker run arguments (e.g. debug options)
ENTRYPOINT ["python3", "app.py"]

