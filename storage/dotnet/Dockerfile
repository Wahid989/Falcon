# FALCON storage service — multi-stage build.
#
# Build context is storage/ (see docker-compose.yml) so the image can carry the
# SQL init scripts, which the service applies at startup.

FROM mcr.microsoft.com/dotnet/sdk:8.0 AS build
WORKDIR /src
COPY dotnet/src/FalconStorage/FalconStorage.csproj dotnet/src/FalconStorage/
RUN dotnet restore dotnet/src/FalconStorage/FalconStorage.csproj
COPY dotnet/src/ dotnet/src/
RUN dotnet publish dotnet/src/FalconStorage/FalconStorage.csproj -c Release -o /app/publish \
    --no-restore /p:UseAppHost=false

FROM mcr.microsoft.com/dotnet/aspnet:8.0
WORKDIR /app
COPY --from=build /app/publish ./
COPY sql/init ./sql/init

# The .NET 8 runtime image listens on 8080 (HTTP_PORTS), matching docker-compose.yml.
EXPOSE 8080
ENV Storage__SchemaScriptsPath=/app/sql/init \
    Storage__LogDirectory=/logs

ENTRYPOINT ["dotnet", "FalconStorage.dll"]
