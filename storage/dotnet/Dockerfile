# FALCON storage service — multi-stage build.
#
# Build stage compiles and publishes the ASP.NET Core project; the runtime
# stage runs the published output on the smaller ASP.NET runtime image.

FROM mcr.microsoft.com/dotnet/sdk:8.0 AS build
WORKDIR /src
COPY FalconStorage.csproj ./
RUN dotnet restore FalconStorage.csproj
COPY . ./
RUN dotnet publish FalconStorage.csproj -c Release -o /app/publish /p:UseAppHost=false

FROM mcr.microsoft.com/dotnet/aspnet:8.0
WORKDIR /app
COPY --from=build /app/publish ./

# Listen on 8080 (matches the port published in docker-compose.yml).
EXPOSE 8080
ENV ASPNETCORE_URLS=http://+:8080

ENTRYPOINT ["dotnet", "FalconStorage.dll"]
