# Minimal .NET image for the storage-api service.
#
# This is a placeholder skeleton: the real .NET log-ingestion service and query
# API are scaffolded later. The ASP.NET runtime image is used here only so the
# container builds and stays up during `docker compose up`. It will be replaced
# by a proper multi-stage build (SDK build stage + runtime stage) once the
# project exists.
FROM mcr.microsoft.com/dotnet/aspnet:8.0

WORKDIR /app

# Compiled service output will be copied here once the project exists.
COPY . /app

# Placeholder: keep the container running without crashing until the real
# service (with its /health endpoint and SQL Server connection) is added.
CMD ["/bin/sh", "-c", "echo 'storage-api service placeholder: idle, waiting for runtime'; tail -f /dev/null"]
