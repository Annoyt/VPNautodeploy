#!/usr/bin/env python3
"""
NekoVPN Interactive Setup

Run this script to deploy NekoVPN from scratch.
Supports both API-based provisioning and existing servers.
"""

import os
import sys
import secrets
import string
from pathlib import Path

# Colors for terminal output
GREEN = "\033[92m"
YELLOW = "\033[93m"
BLUE = "\033[94m"
RED = "\033[91m"
RESET = "\033[0m"


def print_header(text: str):
    """Print section header."""
    print(f"\n{BLUE}{'='*60}{RESET}")
    print(f"{BLUE}{text.center(60)}{RESET}")
    print(f"{BLUE}{'='*60}{RESET}\n")


def print_success(text: str):
    """Print success message."""
    print(f"{GREEN}✓ {text}{RESET}")


def print_info(text: str):
    """Print info message."""
    print(f"{YELLOW}ℹ {text}{RESET}")


def print_error(text: str):
    """Print error message."""
    print(f"{RED}✗ {text}{RESET}")


def generate_password(length=32):
    """Generate random password."""
    alphabet = string.ascii_letters + string.digits + string.punctuation
    return ''.join(secrets.choice(alphabet) for _ in range(length))


def get_input(prompt: str, default: str = None, secret: bool = False) -> str:
    """Get user input with optional default."""
    if default:
        prompt = f"{prompt} [{default}]: "
    else:
        prompt = f"{prompt}: "

    if secret:
        import getpass
        value = getpass.getpass(prompt)
    else:
        value = input(prompt)

    return value or default or ""


class VPNSetup:
    """Main setup orchestrator."""

    def __init__(self):
        self.config = {}
        self.project_root = Path(__file__).parent

    def welcome(self):
        """Show welcome message."""
        print_header("NekoVPN Setup")
        print("This wizard will help you deploy NekoVPN from scratch.")
        print("You can provision new VPS via API or use existing servers.")
        print("\nPress Enter to continue...")
        input()

    def ask_deployment_method(self):
        """Ask how user wants to deploy."""
        print_header("Deployment Method")

        print("Choose deployment method:")
        print("  1. API - Provision new VPS automatically")
        print("  2. Manual - Use existing servers with SSH")

        while True:
            choice = get_input("Select method (1 or 2)", default="2")
            if choice in ["1", "2"]:
                self.config['deployment_method'] = "api" if choice == "1" else "manual"
                break
            print_error("Invalid choice. Enter 1 or 2.")

        print_success(f"Selected: {self.config['deployment_method']}")

    def ask_vps_providers(self):
        """Ask about VPS providers (for API method)."""
        if self.config.get('deployment_method') != 'api':
            return

        print_header("VPS Providers")

        print_info("Entry Node (Russia):")
        print("  Supported providers with API:")
        print("    - adminvps: AdminVPS (Russia-friendly)")
        # TODO: Add more providers as verified

        entry_provider = get_input("Entry node provider", default="adminvps")
        self.config['entry_provider'] = entry_provider

        print_info("\nExit Node (any country):")
        print("  Supported providers with API:")
        print("    - adminvps: AdminVPS")
        print("    - digitalocean: DigitalOcean")
        print("    - hetzner: Hetzner")
        # TODO: Add more providers

        exit_provider = get_input("Exit node provider", default="adminvps")
        self.config['exit_provider'] = exit_provider

        print_success("Providers configured")

    def ask_api_keys(self):
        """Ask for VPS API keys (for API method)."""
        if self.config.get('deployment_method') != 'api':
            return

        print_header("API Keys")

        if self.config.get('entry_provider') == 'adminvps':
            key = get_input(f"AdminVPS API key for entry node", secret=True)
            self.config['entry_api_key'] = key

        if self.config.get('exit_provider') == 'adminvps':
            key = get_input(f"AdminVPS API key for exit node (or same as entry)", secret=True)
            if not key:
                key = self.config.get('entry_api_key', '')
            self.config['exit_api_key'] = key

        # TODO: Add API key prompts for other providers

        print_success("API keys configured")

    def ask_existing_servers(self):
        """Ask for existing server details (for manual method)."""
        if self.config.get('deployment_method') != 'manual':
            return

        print_header("Existing Servers")

        print_info("Entry Node (Russia):")
        self.config['entry_ip'] = get_input("Entry node IP address")
        self.config['entry_ssh_user'] = get_input("SSH user for entry node", default="root")
        self.config['entry_ssh_key'] = get_input("SSH key path", default="~/.ssh/id_rsa")

        print_info("\nExit Node (any country):")
        self.config['exit_ip'] = get_input("Exit node IP address")
        self.config['exit_ssh_user'] = get_input("SSH user for exit node", default="root")
        self.config['exit_ssh_key'] = get_input("SSH key path", default="~/.ssh/id_rsa")

        print_success("Server details configured")

    def ask_telegram_config(self):
        """Ask for Telegram bot configuration."""
        print_header("Telegram Configuration")

        print_info("Get your bot token from @BotFather:")
        print("  1. Open https://t.me/botfather")
        print("  2. Send /newbot")
        print("  3. Follow instructions")
        print("  4. Copy the token (looks like 123456:ABC-DEF...)\n")

        bot_token = get_input("Telegram bot token", secret=True)
        self.config['bot_token'] = bot_token

        print_info("\nGet your Telegram user ID:")
        print("  1. Open https://t.me/userinfobot")
        print("  2. Send /start")
        print("  3. Copy your user ID (numbers only)\n")

        admin_id = get_input("Your Telegram user ID (for admin access)")
        self.config['admin_id'] = admin_id

        print_success("Telegram configured")

    def ask_email_config(self):
        """Ask for email configuration (optional)."""
        print_header("Email Configuration (Optional)")

        print_info("Configure email for backup key delivery?")
        print("Skip if you don't need this feature yet.\n")

        configure = get_input("Configure email now? (y/n)", default="n").lower() == 'y'

        if configure:
            self.config['smtp_host'] = get_input("SMTP host", default="smtp.gmail.com")
            self.config['smtp_port'] = get_input("SMTP port", default="587")
            self.config['smtp_user'] = get_input("SMTP username (email)")
            self.config['smtp_password'] = get_input("SMTP password/app password", secret=True)
            print_success("Email configured")
        else:
            print_info("Email skipped - you can configure later in .env")

    def generate_secrets(self):
        """Generate random secrets for services."""
        print_header("Generating Secrets")

        self.config['xui_username'] = 'admin'
        self.config['xui_password'] = generate_password()
        self.config['db_secret'] = generate_password(16)

        print_success("Secrets generated")

    def write_env_file(self):
        """Write .env file with all configuration."""
        print_header("Writing Configuration")

        env_path = self.project_root / '.env'

        with open(env_path, 'w') as f:
            f.write(f"# Telegram\n")
            f.write(f"BOT_TOKEN={self.config.get('bot_token', '')}\n")
            f.write(f"SUPER_ADMIN_ID={self.config.get('admin_id', '')}\n")
            f.write(f"MODE=production\n")
            f.write(f"FORUM_GROUP_ID=\n")

            f.write(f"\n# Entry Node\n")
            f.write(f"ENTRY_NODE_IP={self.config.get('entry_ip', 'AUTO')}\n")
            f.write(f"REALITY_PUBLIC_KEY=\n"  # Generated after entry deployment

            f.write(f"\n# 3X-UI\n")
            f.write(f"XUI_USERNAME={self.config.get('xui_username', 'admin')}\n")
            f.write(f"XUI_PASSWORD={self.config.get('xui_password', '')}\n")

            f.write(f"\n# Email (optional)\n")
            if self.config.get('smtp_host'):
                f.write(f"SMTP_HOST={self.config.get('smtp_host', '')}\n")
                f.write(f"SMTP_PORT={self.config.get('smtp_port', '587')}\n")
                f.write(f"SMTP_USER={self.config.get('smtp_user', '')}\n")
                f.write(f"SMTP_PASSWORD={self.config.get('smtp_password', '')}\n")

        print_success(f"Configuration written to {env_path}")

    def show_summary(self):
        """Show configuration summary."""
        print_header("Configuration Summary")

        print(f"Deployment method: {self.config.get('deployment_method')}")
        print(f"Bot token: {self.config.get('bot_token', '')[:20]}...")
        print(f"Admin ID: {self.config.get('admin_id', '')}")

        if self.config.get('deployment_method') == 'manual':
            print(f"Entry node: {self.config.get('entry_ip', '')}")
            print(f"Exit node: {self.config.get('exit_ip', '')}")

        print(f"\n3X-UI password: {self.config.get('xui_password', '')}")

        print_info("\nSave this password! You'll need it to access 3X-UI panel.")

    def run(self):
        """Run the full setup wizard."""
        try:
            self.welcome()
            self.ask_deployment_method()
            self.ask_vps_providers()
            self.ask_api_keys()
            self.ask_existing_servers()
            self.ask_telegram_config()
            self.ask_email_config()
            self.generate_secrets()
            self.write_env_file()
            self.show_summary()

            print_header("Setup Complete!")
            print_success("Configuration saved to .env")
            print_info("Next steps:")
            print("  1. Review the .env file")
            print("  2. Run: docker-compose up -d")
            print("  3. Check logs: docker-compose logs -f")

        except KeyboardInterrupt:
            print_error("\nSetup cancelled by user.")
            sys.exit(1)


def main():
    """Entry point."""
    if sys.version_info < (3, 8):
        print_error("Python 3.8+ required")
        sys.exit(1)

    setup = VPNSetup()
    setup.run()


if __name__ == "__main__":
    main()
