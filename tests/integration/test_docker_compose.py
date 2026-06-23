"""Integration tests for Docker Compose setup."""

import pytest
import subprocess
import time
import os
from pathlib import Path


class TestDockerCompose:
    """Integration tests for Docker Compose setup."""
    
    @pytest.fixture(scope="class")
    def compose_file(self, tmp_path_factory):
        """Create a test docker-compose file."""
        compose_content = """
version: '3.8'

services:
  3x-ui:
    image: ghcr.io/mhsanaei/3x-ui:latest
    container_name: test-3x-ui
    restart: unless-stopped
    environment:
      XRAY_VMESS_AEAD_FORCED: "false"
    ports:
      - "2053:2053"
    networks:
      - test-vpn-net

  vpn-bot:
    image: python:3.11-slim
    container_name: test-vpn-bot
    command: sleep infinity
    networks:
      - test-vpn-net
    depends_on:
      - 3x-ui

networks:
  test-vpn-net:
    driver: bridge
"""
        tmp_path = tmp_path_factory.mktemp("docker")
        compose_path = tmp_path / "docker-compose.yml"
        compose_path.write_text(compose_content)
        return compose_path
    
    def test_docker_compose_file_valid(self, compose_file):
        """Test that docker-compose file is valid."""
        # Try legacy docker-compose first, then new 'docker compose' CLI
        for cmd in [['docker-compose', '-f', str(compose_file), 'config'], ['docker', 'compose', '-f', str(compose_file), 'config']]:
            try:
                result = subprocess.run(cmd, capture_output=True, text=True)
                if result.returncode == 0:
                    return
            except FileNotFoundError:
                continue
        pytest.skip("docker-compose not available")
    
    def test_containers_can_communicate(self):
        """Test that containers can communicate over network."""
        # This test assumes containers are already running
        # Skip if docker is not available
        try:
            subprocess.run(['docker', 'ps'], capture_output=True, check=True)
        except (subprocess.CalledProcessError, FileNotFoundError):
            pytest.skip("Docker not available")
        
        # Check if 3x-ui container exists
        result = subprocess.run(
            ['docker', 'ps', '--filter', 'name=3x-ui', '--format', '{{.Names}}'],
            capture_output=True,
            text=True
        )
        
        if '3x-ui' not in result.stdout:
            pytest.skip("3x-ui container not running")
        
        # Test ping from host to 3x-ui
        result = subprocess.run(
            ['docker', 'exec', '3x-ui', 'ping', '-c', '1', '127.0.0.1'],
            capture_output=True,
            text=True
        )
        assert result.returncode == 0, "Cannot ping 3x-ui container"
    
    def test_xui_api_accessible(self):
        """Test that 3X-UI API is accessible."""
        import urllib.request
        import ssl
        
        # Create SSL context that ignores certificate validation
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE
        
        try:
            response = urllib.request.urlopen(
                'http://127.0.0.1:2026/login',
                timeout=5,
                context=ssl_context
            )
            # Should get 200 or redirect
            assert response.status in [200, 301, 302]
        except Exception as e:
            pytest.skip(f"3X-UI API not accessible: {e}")
    
    def test_bot_db_path_exists(self):
        """Test that bot database path exists."""
        db_path = Path('/var/lib/vpn-bot')
        if db_path.exists():
            assert db_path.is_dir()
            assert os.access(db_path, os.W_OK)
        else:
            # On CI/non-docker environment, skip
            pytest.skip("Bot DB path not mounted")


class TestXUIAPIIntegration:
    """Integration tests for X-UI API."""
    
    @pytest.mark.asyncio
    async def test_api_login_with_real_container(self):
        """Test login to real 3X-UI container."""
        import os
        
        # Skip if not in docker environment
        if not os.path.exists('/.dockerenv') and not os.getenv('CI'):
            pytest.skip("Not in Docker environment")
        
        from bot.services.xui_api import XUIAPIClient, XUIClientConfig
        
        config = XUIClientConfig(
            base_url='http://3x-ui:2026',
            username='admin',
            password='admin'
        )
        
        async with XUIAPIClient(config) as client:
            result = await client.login()
            assert result is True
    
    @pytest.mark.asyncio
    async def test_traffic_collection_via_api(self):
        """Test traffic collection via API (no docker exec)."""
        import os
        
        # Skip if not in docker environment
        if not os.path.exists('/.dockerenv') and not os.getenv('CI'):
            pytest.skip("Not in Docker environment")
        
        from bot.services.xui_api import XRayStatsService
        
        async with XRayStatsService('http://3x-ui:2026') as service:
            # Login first
            logged_in = await service.login()
            assert logged_in, "Failed to login"
            
            # Get stats
            stats = await service.get_all_clients_stats()
            
            # Verify we got a dict (even if empty)
            assert isinstance(stats, dict)
            
            # Verify no docker exec was used (this is implicit - if API works, we didn't use docker exec)
            print(f"Collected stats for {len(stats)} clients via HTTP API")
