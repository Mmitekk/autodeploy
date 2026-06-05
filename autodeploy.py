#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
=============================================================================
  AutoDeploy — Автоматический деплой сайтов
=============================================================================

  Запуск:
    python3 autodeploy.py

  15 блоков деплоя. Большинство — автоматические.
  Пользователь вводит только: название проекта, путь, настройки БД,
  имя репозитория, токен GitHub.

=============================================================================
"""

import glob
import json
import os
import random
import socket
import string
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional, Callable, Dict, Any, Tuple

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt, Confirm
from rich.table import Table
from rich.text import Text
from rich import box
from rich.rule import Rule

console = Console()


# ╔═══════════════════════════════════════════════════════════════════════════╗
# ║  УТИЛИТЫ                                                                 ║
# ╚═══════════════════════════════════════════════════════════════════════════╝

def run_cmd(cmd: str, timeout: int = 30) -> Tuple[int, str, str]:
    """Выполняет shell-команду. Возвращает (returncode, stdout, stderr)."""
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=timeout
        )
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except subprocess.TimeoutExpired:
        return -1, "", "Таймаут команды"
    except Exception as e:
        return -2, "", str(e)


def find_free_port(start: int = 3000, end: int = 9999, exclude: set = None) -> int:
    """Находит свободный порт в диапазоне."""
    exclude = exclude or set()
    _, out, _ = run_cmd("ss -tlnp 2>/dev/null | awk '{print $4}' | grep -oP '\\d+$' | sort -u")
    used_ports = set()
    for p in out.split('\n'):
        try:
            used_ports.add(int(p.strip()))
        except (ValueError, AttributeError):
            pass
    used_ports.update(exclude)

    for port in range(start, end):
        if port in used_ports:
            continue
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(1)
            s.bind(('0.0.0.0', port))
            s.close()
            return port
        except (OSError, socket.error):
            continue
    return random.randint(49000, 59999)


def find_db_files(project_dir: str, max_depth: int = 3) -> List[str]:
    """Ищет *.db файлы в директории проекта до max_depth уровней."""
    results = []
    for depth in range(1, max_depth + 1):
        pattern = os.path.join(project_dir, *(['*'] * depth), '*.db')
        results.extend(glob.glob(pattern))
    results.extend(glob.glob(os.path.join(project_dir, '*.db')))
    results = sorted(set(results))
    db_subdir = [r for r in results if '/db/' in r]
    other = [r for r in results if '/db/' not in r]
    return db_subdir + other


def generate_password(length: int = 20) -> str:
    """Генерирует случайный пароль."""
    chars = string.ascii_letters + string.digits + "!@#$%^&*"
    return ''.join(random.choice(chars) for _ in range(length))


def get_site_domain(site_path: str) -> str:
    """Извлекает домен из пути к сайту."""
    return os.path.basename(site_path.rstrip('/'))


# ╔═══════════════════════════════════════════════════════════════════════════╗
# ║  РЕЗУЛЬТАТ БЛОКА                                                         ║
# ╚═══════════════════════════════════════════════════════════════════════════╝

@dataclass
class BlockResult:
    block_id: str
    block_num: int
    title: str
    success: bool
    message: str = ""
    details: str = ""
    data: Dict[str, Any] = field(default_factory=dict)


# ╔═══════════════════════════════════════════════════════════════════════════╗
# ║  ИСПОЛНИТЕЛЬ БЛОКОВ — ФАКТИЧЕСКАЯ ЛОГИКА ДЕПЛОЯ                         ║
# ╚═══════════════════════════════════════════════════════════════════════════╝

class DeployExecutor:
    """Исполнитель блоков деплоя."""

    def __init__(self, context: Dict[str, Any]):
        self.ctx = context
        self.results: List[BlockResult] = []
        self.used_ports: set = set()

    def _add_result(self, block_num: int, block_id: str, title: str,
                    success: bool, message: str = "", details: str = "",
                    data: Dict = None) -> BlockResult:
        r = BlockResult(
            block_id=block_id, block_num=block_num, title=title,
            success=success, message=message, details=details, data=data or {}
        )
        self.results.append(r)
        return r

    # ── Блок 1: Проект и путь ────────────────────────────────────────────

    def execute_block1(self) -> BlockResult:
        project_name = self.ctx.get("project_name", "").strip()
        site_path = self.ctx.get("site_path", "").strip()

        if not project_name:
            return self._add_result(1, "project", "Название проекта и путь", False,
                                    "Название проекта не указано")
        if not site_path:
            return self._add_result(1, "project", "Название проекта и путь", False,
                                    "Путь к сайту не указан")

        if os.path.exists(site_path):
            self.ctx["site_path"] = site_path
            self.ctx["project_name"] = project_name
            self.ctx["domain"] = get_site_domain(site_path)
            return self._add_result(1, "project", "Название проекта и путь", True,
                                    f"Проект: {project_name}",
                                    f"Путь: {site_path}\nДомен: {self.ctx['domain']}")
        else:
            try:
                os.makedirs(site_path, exist_ok=True)
                self.ctx["site_path"] = site_path
                self.ctx["project_name"] = project_name
                self.ctx["domain"] = get_site_domain(site_path)
                return self._add_result(1, "project", "Название проекта и путь", True,
                                        f"Проект: {project_name} (путь создан)",
                                        f"Путь: {site_path}\nДомен: {self.ctx['domain']}")
            except Exception as e:
                return self._add_result(1, "project", "Название проекта и путь", False,
                                        f"Не удалось создать путь: {site_path}", str(e))

    # ── Блок 2: Свободный порт (авто) ────────────────────────────────────

    def execute_block2(self) -> BlockResult:
        port = find_free_port(3000, 9999, self.used_ports)
        self.ctx["app_port"] = port
        self.used_ports.add(port)
        return self._add_result(2, "port_select", "Выбор свободного порта", True,
                                f"Порт: {port}")

    # ── Блок 3: Режим запуска (Standalone / Standard) ──────────────────

    def execute_block3(self) -> BlockResult:
        site_path = self.ctx.get("site_path", ".")
        use_standalone = self.ctx.get("use_standalone", "auto")

        # Автодетект standalone в next.config
        has_standalone_config = False
        for cfg in ["next.config.ts", "next.config.js", "next.config.mjs"]:
            cfg_path = os.path.join(site_path, cfg)
            if os.path.exists(cfg_path):
                try:
                    with open(cfg_path, 'r') as f:
                        content = f.read()
                    if 'standalone' in content:
                        has_standalone_config = True
                        break
                except Exception:
                    pass

        # Проверяем .next/standalone/server.js
        has_standalone_build = False
        for candidate in [
            os.path.join(site_path, ".next", "standalone", "server.js"),
        ]:
            if os.path.exists(candidate):
                has_standalone_build = True
                break
        # Также ищем рекурсивно
        if not has_standalone_build:
            standalone_dir = os.path.join(site_path, ".next", "standalone")
            if os.path.isdir(standalone_dir):
                for root, dirs, files in os.walk(standalone_dir):
                    if "server.js" in files:
                        has_standalone_build = True
                        break

        if use_standalone == "1" or use_standalone == "standalone":
            self.ctx["use_standalone"] = "yes"
            mode_desc = "Standalone (node server.js)"
        elif use_standalone == "2" or use_standalone == "standard":
            self.ctx["use_standalone"] = "no"
            mode_desc = "Standard (npx next start)"
        else:
            # Авто
            if has_standalone_config or has_standalone_build:
                self.ctx["use_standalone"] = "yes"
                mode_desc = "Standalone (автоопределение)"
            else:
                self.ctx["use_standalone"] = "no"
                mode_desc = "Standard (автоопределение)"

        details = []
        if has_standalone_config:
            details.append("Обнаружен output: standalone в конфиге")
        if has_standalone_build:
            details.append("Обнаружен .next/standalone/server.js")

        return self._add_result(3, "run_mode", "Режим запуска", True,
                                mode_desc,
                                '\n'.join(details) if details else "")

    # ── Блок 4: Домен и SSL ─────────────────────────────────────────────

    def execute_block4(self) -> BlockResult:
        site_domain = self.ctx.get("site_domain", "")
        use_ssl = self.ctx.get("use_ssl", "no")

        if not site_domain:
            site_domain = self.ctx.get("domain", "")

        self.ctx["site_domain"] = site_domain
        self.ctx["use_ssl"] = use_ssl == "yes"

        if site_domain:
            return self._add_result(4, "domain_ssl", "Домен и SSL", True,
                                    f"Домен: {site_domain}, SSL: {'Да' if use_ssl == 'yes' else 'Нет'}")
        else:
            return self._add_result(4, "domain_ssl", "Домен и SSL", True,
                                    "Домен не указан — Apache не настраивается")

    # ── Блок 5: PM2 (авто) ───────────────────────────────────────────────

    def execute_block5(self) -> BlockResult:
        project_name = self.ctx.get("project_name", "app")
        site_path = self.ctx.get("site_path", ".")
        app_port = self.ctx.get("app_port", 3000)
        use_standalone = self.ctx.get("use_standalone", "no")

        rc, _, _ = run_cmd("which pm2")
        if rc != 0:
            return self._add_result(5, "pm2_setup", "Подготовка PM2 конфигурации", False,
                                    "pm2 не установлен",
                                    "Установите: npm install -g pm2")

        # Определяем скрипт запуска в зависимости от режима
        if use_standalone == "yes":
            pm2_script = "node"
            pm2_args = ".next/standalone/server.js"
            mode_label = "Standalone"
        else:
            pm2_script = "npm"
            pm2_args = "start"
            mode_label = "Standard"

        # Сохраняем параметры запуска для Block 11
        self.ctx["pm2_script"] = pm2_script
        self.ctx["pm2_args"] = pm2_args
        self.ctx["pm2_mode_label"] = mode_label

        ecosystem_content = f"""module.exports = {{
  apps: [{{
    name: '{project_name}',
    script: '{pm2_script}',
    args: '{pm2_args}',
    cwd: '{site_path}',
    env: {{
      PORT: {app_port},
      NODE_ENV: '{self.ctx.get("deploy_mode", "production").lower()}',
      HOSTNAME: '0.0.0.0'
    }},
    instances: 1,
    autorestart: true,
    watch: false,
    max_memory_restart: '500M',
    error_file: '{site_path}/logs/pm2-error.log',
    out_file: '{site_path}/logs/pm2-out.log',
    merge_logs: true,
    log_date_format: 'YYYY-MM-DD HH:mm:ss'
  }}]
}};
"""
        ecosystem_path = os.path.join(site_path, "ecosystem.config.js")
        try:
            os.makedirs(os.path.dirname(ecosystem_path), exist_ok=True)
            with open(ecosystem_path, 'w') as f:
                f.write(ecosystem_content)
            return self._add_result(5, "pm2_setup", "Подготовка PM2 конфигурации", True,
                                    f"Конфиг создан: {mode_label}",
                                    f"Режим: {mode_label}\nПорт: {app_port}\nФайл: {ecosystem_path}")
        except Exception as e:
            return self._add_result(5, "pm2_setup", "Подготовка PM2 конфигурации", False,
                                    f"Ошибка: {e}")

    # ── Блок 6: Веб-сервер (авто) ──────────────────────────────────────

    def _detect_port80_server(self) -> str:
        """Определяет, какой веб-сервер слушает порт 80."""
        rc, out, _ = run_cmd("ss -tlnp 'sport = :80' 2>/dev/null")
        if rc == 0 and out:
            if 'nginx' in out.lower():
                return "nginx"
            if 'apache' in out.lower() or 'httpd' in out.lower():
                return "apache"
        # Проверяем процессы
        rc, out, _ = run_cmd("ps aux | grep -E '[n]ginx|[a]pache2|[h]ttpd' | head -5")
        if rc == 0 and out:
            if 'nginx' in out.lower():
                return "nginx"
            if 'apache' in out.lower() or 'httpd' in out.lower():
                return "apache"
        # Fallback: наличие директорий
        if os.path.isdir("/etc/nginx/sites-enabled"):
            return "nginx"
        if os.path.isdir("/etc/apache2/sites-enabled"):
            return "apache"
        return "unknown"

    def _config_has_proxy(self, config_path: str, port: int) -> bool:
        """Проверяет, содержит ли конфиг прокси на нужный порт."""
        try:
            with open(config_path, 'r') as f:
                content = f.read().lower()
            return str(port) in content and (
                'proxypass' in content or 'proxy_pass' in content
            )
        except Exception:
            return False

    def _update_nginx_config(self, domain: str, site_path: str, app_port: int) -> BlockResult:
        """Создаёт или обновляет конфиг Nginx."""
        nginx_sites = "/etc/nginx/sites-available"
        nginx_enabled = "/etc/nginx/sites-enabled"

        config_path = os.path.join(nginx_sites, domain)
        existing_proxy = False

        if os.path.exists(config_path):
            existing_proxy = self._config_has_proxy(config_path, app_port)

        if existing_proxy:
            return self._add_result(6, "web_server", "Конфигурация веб-сервера", True,
                                    f"Nginx: конфиг уже настроен ({config_path})",
                                    f"Прокси на порт {app_port}")

        # Создаём / перезаписываем конфиг
        vhost = f"""server {{
    listen 80;
    server_name {domain} www.{domain};

    location / {{
        proxy_pass http://127.0.0.1:{app_port};
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection 'upgrade';
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_cache_bypass $http_upgrade;
    }}

    location /_next/static/ {{
        alias {site_path}/.next/static/;
        expires 365d;
        access_log off;
    }}

    error_log /var/log/nginx/{domain}_error.log;
    access_log /var/log/nginx/{domain}_access.log;
}}
"""
        try:
            with open(config_path, 'w') as f:
                f.write(vhost)
            # Создаём симлинк
            enabled_link = os.path.join(nginx_enabled, domain)
            if not os.path.exists(enabled_link):
                run_cmd(f"ln -sf {config_path} {enabled_link}")
            # Удаляем default если мешает
            default_conf = os.path.join(nginx_enabled, "default")
            if os.path.exists(default_conf):
                run_cmd(f"mv {default_conf} {default_conf}.bak 2>/dev/null")
            run_cmd("systemctl reload nginx")
            action = "Обновлён" if os.path.exists(config_path) else "Создан"
            return self._add_result(6, "web_server", "Конфигурация веб-сервера", True,
                                    f"Nginx: {action} конфиг для {domain}",
                                    f"Прокси на порт {app_port}")
        except Exception as e:
            return self._add_result(6, "web_server", "Конфигурация веб-сервера", False,
                                    f"Ошибка Nginx: {e}")

    def _update_apache_config(self, domain: str, site_path: str, app_port: int) -> BlockResult:
        """Создаёт или обновляет конфиг Apache."""
        apache_sites = "/etc/apache2/sites-available"
        apache_enabled = "/etc/apache2/sites-enabled"

        config_path = os.path.join(apache_sites, f"{domain}.conf")
        existing_proxy = False

        if os.path.exists(config_path):
            existing_proxy = self._config_has_proxy(config_path, app_port)

        if existing_proxy:
            return self._add_result(6, "web_server", "Конфигурация веб-сервера", True,
                                    f"Apache: конфиг уже настроен ({config_path})",
                                    f"Прокси на порт {app_port}")

        # Создаём / перезаписываем конфиг
        vhost = f"""<VirtualHost *:80>
    ServerName {domain}
    ServerAlias www.{domain}
    DocumentRoot {site_path}/public

    ProxyPreserveHost On
    ProxyPass / http://127.0.0.1:{app_port}/
    ProxyPassReverse / http://127.0.0.1:{app_port}/

    <Directory {site_path}/public>
        Options -Indexes +FollowSymLinks
        AllowOverride All
        Require all granted
    </Directory>

    ErrorLog ${{APACHE_LOG_DIR}}/{domain}_error.log
    CustomLog ${{APACHE_LOG_DIR}}/{domain}_access.log combined
</VirtualHost>
"""
        try:
            with open(config_path, 'w') as f:
                f.write(vhost)
            run_cmd(f"a2ensite {domain}.conf 2>/dev/null")
            # Включаем proxy-модули
            run_cmd("a2enmod proxy proxy_http 2>/dev/null")
            run_cmd("systemctl reload apache2")
            action = "Обновлён" if os.path.exists(config_path) else "Создан"
            return self._add_result(6, "web_server", "Конфигурация веб-сервера", True,
                                    f"Apache: {action} конфиг для {domain}",
                                    f"Прокси на порт {app_port}")
        except Exception as e:
            return self._add_result(6, "web_server", "Конфигурация веб-сервера", False,
                                    f"Ошибка Apache: {e}")

    def execute_block6(self) -> BlockResult:
        domain = self.ctx.get("domain", "")
        site_path = self.ctx.get("site_path", ".")
        app_port = self.ctx.get("app_port", 3000)

        if not domain:
            return self._add_result(6, "web_server", "Конфигурация веб-сервера", False,
                                    "Домен не определён")

        # Сохраняем информацию о веб-сервере
        port80_server = self._detect_port80_server()
        self.ctx["port80_server"] = port80_server

        # Настраиваем фронтенд-сервер (тот что слушает порт 80)
        if port80_server == "nginx":
            result = self._update_nginx_config(domain, site_path, app_port)
        elif port80_server == "apache":
            result = self._update_apache_config(domain, site_path, app_port)
        else:
            # Не определили — пробуем оба
            if os.path.isdir("/etc/nginx/sites-available"):
                result = self._update_nginx_config(domain, site_path, app_port)
            elif os.path.isdir("/etc/apache2/sites-available"):
                result = self._update_apache_config(domain, site_path, app_port)
            else:
                result = self._add_result(6, "web_server", "Конфигурация веб-сервера", False,
                                          "Ни Apache, ни Nginx не найдены")

        return result

    # ── Блок 7: Репозиторий ──────────────────────────────────────────────

    def execute_block7(self) -> BlockResult:
        repo_name = self.ctx.get("repo_name", "").strip()
        github_token = self.ctx.get("github_token", "").strip()
        repo_private = self.ctx.get("repo_private", True)

        if not repo_name:
            return self._add_result(7, "git_repo", "Git-репозиторий", False,
                                    "Имя репозитория не указано")

        self.ctx["repo_name"] = repo_name
        self.ctx["github_token"] = github_token
        self.ctx["repo_private"] = repo_private

        details = f"Токен: {'указан' if github_token else 'не указан'}\n"
        details += f"Приватный: {'да' if repo_private else 'нет'}"

        return self._add_result(7, "git_repo", "Git-репозиторий", True,
                                f"Репозиторий: {repo_name}",
                                details)

    # ── Блок 8: Поиск *.db (авто) ────────────────────────────────────────

    def execute_block8(self) -> BlockResult:
        site_path = self.ctx.get("site_path", ".")
        db_files = find_db_files(site_path, max_depth=3)

        if db_files:
            selected = db_files[0]
            self.ctx["db_file"] = selected
            rel_path = os.path.relpath(selected, site_path)
            return self._add_result(8, "db_file_find", "Поиск файла базы данных", True,
                                    f"Найдена БД: {rel_path}",
                                    f"Полный путь: {selected}\nВсего *.db: {len(db_files)}",
                                    data={"db_file": selected, "all_db_files": db_files})
        else:
            self.ctx["db_file"] = ""
            return self._add_result(8, "db_file_find", "Поиск файла базы данных", True,
                                    "*.db файлы не найдены",
                                    "База данных не обнаружена в директории проекта",
                                    data={"db_file": "", "all_db_files": []})

    # ── Блок 9: .gitignore (авто) ────────────────────────────────────────

    def execute_block9(self) -> BlockResult:
        site_path = self.ctx.get("site_path", ".")

        # .gitignore как в оригинальном скрипте
        entries = [
            "node_modules/",
            ".next/",
            "out/",
            "*.db",
            "*.db-journal",
            "*.db-wal",
            "*.db-shm",
            ".env",
            ".env.local",
            ".env.*.local",
            "*.log",
            "npm-debug.log*",
            ".DS_Store",
            "Thumbs.db",
            "*.tmp",
            "*.bak",
            "*.swp",
            ".vscode/",
            ".idea/",
            "*.pem",
            "*.key",
            "*.crt",
            "setup-git-deploy.sh",
            "webhook-server.js",
            "deploy.sh",
            ".env.webhook",
            "ecosystem.config.js",
            "logs/",
        ]

        gitignore_path = os.path.join(site_path, ".gitignore")
        try:
            os.makedirs(os.path.dirname(gitignore_path), exist_ok=True)
            with open(gitignore_path, 'w') as f:
                f.write('\n'.join(entries) + '\n')
            return self._add_result(9, "gitignore", "Создание .gitignore", True,
                                    f"Создан: {gitignore_path}")
        except Exception as e:
            return self._add_result(9, "gitignore", "Создание .gitignore", False,
                                    f"Ошибка: {e}")

    # ── Блок 10: .env (авто) ─────────────────────────────────────────────

    def execute_block10(self) -> BlockResult:
        site_path = self.ctx.get("site_path", ".")
        app_port = self.ctx.get("app_port", 3000)
        db_file = self.ctx.get("db_file", "")
        site_domain = self.ctx.get("site_domain", "")
        use_ssl = self.ctx.get("use_ssl", False)
        project_name = self.ctx.get("project_name", "app")

        # Читаем существующий .env если есть — сохраняем пользовательские переменные
        existing_env = {}
        env_path = os.path.join(site_path, ".env")
        if os.path.exists(env_path):
            try:
                with open(env_path, 'r') as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith('#') and '=' in line:
                            k, v = line.split('=', 1)
                            existing_env[k.strip()] = v.strip()
            except Exception:
                pass

        # Формируем .env с SQLite (как в оригинальном скрипте)
        env_lines = [
            "# Application",
            f"APP_NAME={project_name}",
            f"APP_PORT={app_port}",
            f"NODE_ENV=production",
            "",
            "# Server",
            f"HOST=0.0.0.0",
            f"PORT={app_port}",
        ]

        # Database — SQLite
        if db_file:
            rel_db = os.path.relpath(db_file, site_path)
            env_lines.extend([
                "",
                "# Database (SQLite)",
                f"DATABASE_URL=\"file:./{rel_db}\"",
                f"DB_FILE={db_file}",
            ])
        else:
            env_lines.extend([
                "",
                "# Database (SQLite)",
                f"DATABASE_URL=\"file:./db/{project_name}.db\"",
            ])

        # Site URL
        if site_domain:
            proto = "https" if use_ssl else "http"
            env_lines.extend([
                "",
                "# Site",
                f"NEXT_PUBLIC_SITE_URL={proto}://{site_domain}",
            ])
        else:
            env_lines.extend([
                "",
                "# Site",
                f"NEXT_PUBLIC_SITE_URL=http://localhost:{app_port}",
            ])

        # Security
        env_lines.extend([
            "",
            "# Security",
            f"SECRET_KEY={generate_password(32)}",
        ])

        # SSL
        env_lines.extend([
            "",
            "# SSL",
            f"ENABLE_SSL={'true' if use_ssl else 'false'}",
        ])

        # Добавляем пользовательские переменные из существующего .env
        known_keys = set()
        for line in env_lines:
            if '=' in line and not line.startswith('#'):
                known_keys.add(line.split('=', 1)[0].strip())

        custom_keys = {k: v for k, v in existing_env.items() if k not in known_keys}
        if custom_keys:
            env_lines.append("")
            env_lines.append("# User variables (preserved)")
            for k, v in custom_keys.items():
                env_lines.append(f"{k}={v}")

        env = '\n'.join(env_lines) + '\n'

        try:
            os.makedirs(os.path.dirname(env_path), exist_ok=True)
            with open(env_path, 'w') as f:
                f.write(env)
            return self._add_result(10, "env_file", "Создание .env", True,
                                    f"Создан: {env_path}")
        except Exception as e:
            return self._add_result(10, "env_file", "Создание .env", False, f"Ошибка: {e}")

    # ── Блок 11: Сборка и запуск ───────────────────────────────────────────

    def execute_block11(self) -> BlockResult:
        site_path = self.ctx.get("site_path", ".")
        project_name = self.ctx.get("project_name", "app")
        use_standalone = self.ctx.get("use_standalone", "no")
        steps = []
        has_error = False

        # 1. Создаём директории
        for d in ["logs", "public", "tmp"]:
            dp = os.path.join(site_path, d)
            os.makedirs(dp, exist_ok=True)
            steps.append(f"Создана: {d}/")

        # 2. Git init
        git_dir = os.path.join(site_path, ".git")
        if not os.path.exists(git_dir):
            rc, _, err = run_cmd(f"cd {site_path} && git init")
            steps.append("Git init" + ("" if rc == 0 else f": {err[:80]}"))
        else:
            steps.append("Git уже инициализирован")

        # 3. npm install (С dev-зависимостями — нужно для next build)
        pkg = os.path.join(site_path, "package.json")
        if os.path.exists(pkg):
            node_modules = os.path.join(site_path, "node_modules")
            # Проверяем, не повреждён ли node_modules (битый SWC и т.п.)
            swc_file = os.path.join(node_modules, "@next", "swc-linux-x64-gnu",
                                    "next-swc.linux-x64-gnu.node")
            node_modules_ok = os.path.isdir(node_modules)
            if node_modules_ok and os.path.exists(swc_file):
                try:
                    swc_size = os.path.getsize(swc_file)
                    if swc_size < 1_000_000:  # < 1МБ — битый (должен быть ~130МБ)
                        console.print(f"  [yellow]SWC бинарник повреждён ({swc_size} байт), переустанавливаю...[/yellow]")
                        node_modules_ok = False
                except OSError:
                    node_modules_ok = False

            if not node_modules_ok:
                # Полная очистка и установка с нуля
                console.print("  [cyan]Очистка и установка зависимостей...[/cyan]")
                run_cmd(f"cd {site_path} && rm -rf node_modules package-lock.json")
                rc, out, err = run_cmd(
                    f"cd {site_path} && npm install --legacy-peer-deps", timeout=300
                )
                if rc == 0:
                    steps.append("npm install --legacy-peer-deps — OK")
                else:
                    steps.append(f"npm install — ошибка: {err[:200]}")
                    has_error = True
            else:
                steps.append("node_modules — уже установлены")

        # 4. next build (сборка проекта)
        if not has_error and os.path.exists(pkg):
            # Проверяем, нужна ли сборка (есть ли .next/standalone/server.js)
            standalone_path = os.path.join(site_path, ".next", "standalone", "server.js")
            next_dir = os.path.join(site_path, ".next")

            need_build = not os.path.isdir(next_dir)
            if not need_build and use_standalone == "yes" and not os.path.exists(standalone_path):
                need_build = True

            if need_build:
                console.print("  [cyan]Сборка проекта (next build)...[/cyan]")
                # Ограничиваем память Node.js для стабильности
                build_cmd = (f"cd {site_path} && "
                             f"NODE_OPTIONS='--max-old-space-size=4096' npm run build")
                rc, out, err = run_cmd(build_cmd, timeout=600)
                if rc == 0:
                    steps.append("next build — OK")
                else:
                    # Проверяем, может быть Bus error (OOM) — пробуем с меньшим лимитом
                    err_text = (err or out or "")[-500:]
                    if 'Bus error' in err_text or 'core dumped' in err_text or rc == -1:
                        console.print("  [yellow]Build упал (возможно OOM), пробую с 2ГБ лимитом...[/yellow]")
                        build_cmd2 = (f"cd {site_path} && "
                                      f"NODE_OPTIONS='--max-old-space-size=2048' npm run build")
                        rc2, out2, err2 = run_cmd(build_cmd2, timeout=600)
                        if rc2 == 0:
                            steps.append("next build (2ГБ лимит) — OK")
                            rc, err = rc2, err2
                        else:
                            steps.append("next build — ошибка (не хватает памяти?)")
                            steps.append(f"  {(err2 or err or '')[-300:]}")
                            has_error = True
                    else:
                        steps.append("next build — ошибка")
                        steps.append(f"  {err_text[-300:]}")
                        has_error = True
            else:
                steps.append(".next — уже собран, сборка не нужна")

            # Проверяем standalone после сборки
            if not has_error and use_standalone == "yes":
                if os.path.exists(standalone_path):
                    steps.append("standalone server.js — создан")
                else:
                    steps.append("standalone server.js — НЕ НАЙДЕН, переключаюсь на Standard")
                    self.ctx["use_standalone"] = "no"
                    # Пересоздаём ecosystem.config.js для Standard режима
                    app_port = self.ctx.get("app_port", 3000)
                    eco_content = f"""module.exports = {{
  apps: [{{
    name: '{project_name}',
    script: 'npm',
    args: 'start',
    cwd: '{site_path}',
    env: {{
      PORT: {app_port},
      NODE_ENV: 'production',
      HOSTNAME: '0.0.0.0'
    }},
    instances: 1,
    autorestart: true,
    watch: false,
    max_memory_restart: '500M',
    error_file: '{site_path}/logs/pm2-error.log',
    out_file: '{site_path}/logs/pm2-out.log',
    merge_logs: true,
    log_date_format: 'YYYY-MM-DD HH:mm:ss'
  }}]
}};
"""
                    eco_path = os.path.join(site_path, "ecosystem.config.js")
                    with open(eco_path, 'w') as f:
                        f.write(eco_content)

        # 5. Запуск PM2 (после сборки!)
        if not has_error:
            # Удаляем старый процесс если есть
            run_cmd(f"pm2 delete {project_name} 2>/dev/null")

            rc, out, err = run_cmd(f"cd {site_path} && pm2 start ecosystem.config.js")
            if rc == 0:
                run_cmd("pm2 save")
                mode_label = self.ctx.get("pm2_mode_label", "Standard")
                steps.append(f"PM2 запущен ({mode_label})")
            else:
                steps.append(f"PM2 — ошибка: {err[:150]}")
                has_error = True

        msg = "Завершена" if not has_error else "Завершена с ошибками"
        return self._add_result(11, "auto_setup", "Сборка и запуск", not has_error,
                                msg, '\n'.join(steps))

    # ── Блок 12: Проверка репо и токена (check) ──────────────────────────

    def execute_block12(self) -> BlockResult:
        repo_name = self.ctx.get("repo_name", "")
        github_token = self.ctx.get("github_token", "")
        github_token_retry = self.ctx.get("github_token_retry", "")
        site_path = self.ctx.get("site_path", ".")

        if github_token_retry:
            github_token = github_token_retry
            self.ctx["github_token"] = github_token
            self.ctx["github_token_retry"] = ""

        if not repo_name:
            return self._add_result(12, "repo_check", "Проверка репозитория и токена", False,
                                    "Имя репозитория не задано")
        if not github_token:
            return self._add_result(12, "repo_check", "Проверка репозитория и токена", False,
                                    "GitHub Token не указан")

        rc, out, err = run_cmd(
            f"curl -s -o /dev/null -w '%{{http_code}}' "
            f"-H 'Authorization: token {github_token}' "
            f"https://api.github.com/repos/{repo_name}"
        )

        if rc == 0:
            status = out.strip().replace("'", "")
            if status == "200":
                run_cmd(f"cd {site_path} && git remote get-url origin 2>/dev/null")
                run_cmd(
                    f"cd {site_path} && git remote set-url origin "
                    f"https://{github_token}@github.com/{repo_name}.git 2>/dev/null || "
                    f"git remote add origin https://{github_token}@github.com/{repo_name}.git"
                )
                return self._add_result(12, "repo_check", "Проверка репозитория и токена", True,
                                        f"Репозиторий {repo_name} доступен")
            elif status == "404":
                return self._add_result(12, "repo_check", "Проверка репозитория и токена", False,
                                        f"Репозиторий {repo_name} не найден",
                                        "Проверьте имя и права токена")
            elif status == "401":
                return self._add_result(12, "repo_check", "Проверка репозитория и токена", False,
                                        "Токен GitHub неверен или истёк",
                                        "Нужен новый токен с правами repo")
            else:
                return self._add_result(12, "repo_check", "Проверка репозитория и токена", False,
                                        f"Неожиданный ответ: {status}", err[:200])
        else:
            return self._add_result(12, "repo_check", "Проверка репозитория и токена", False,
                                    "Не удалось подключиться к GitHub API", err[:200])

    # ── Блок 13: Первый коммит (авто) ────────────────────────────────────

    def execute_block13(self) -> BlockResult:
        site_path = self.ctx.get("site_path", ".")
        project_name = self.ctx.get("project_name", "app")
        repo_name = self.ctx.get("repo_name", "")
        github_token = self.ctx.get("github_token", "")

        rc, _, err = run_cmd(f"cd {site_path} && git add -A")
        if rc != 0:
            return self._add_result(13, "first_commit", "Первый коммит и пуш", False,
                                    "Ошибка git add", err[:200])

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        commit_msg = f"AutoDeploy: initial commit for {project_name} [{timestamp}]"

        rc, out, err = run_cmd(f'cd {site_path} && git commit -m "{commit_msg}"')
        if rc != 0 and "nothing to commit" not in out:
            return self._add_result(13, "first_commit", "Первый коммит и пуш", False,
                                    "Ошибка git commit", f"{out}\n{err}"[:300])

        if repo_name and github_token:
            # Устанавливаем remote URL с токеном
            run_cmd(
                f"cd {site_path} && git remote set-url origin "
                f"https://{github_token}@github.com/{repo_name}.git"
            )

            # Убежимся, что локальная ветка называется 'main'
            # (git init создаёт master, а remote обычно использует main)
            rc_branch, out_branch, _ = run_cmd(
                f"cd {site_path} && git branch --show-current"
            )
            current_branch = out_branch.strip() or "master"
            if current_branch != "main":
                run_cmd(f"cd {site_path} && git branch -m {current_branch} main")

            # Подтягиваем изменения с remote (если remote не пустой)
            # -X ours: при конфликтах — предпочтение локальным файлам
            run_cmd(
                f"cd {site_path} && git pull origin main "
                f"--rebase --allow-unrelated-histories -X ours 2>&1"
            )
            # Ошибки pull не критичны (remote может быть пустым или без ветки main)

            # Пробуем обычный push
            rc, out, err = run_cmd(
                f"cd {site_path} && git push -u origin main 2>&1"
            )
            if rc != 0 and "Everything up-to-date" not in out:
                # Если push отклонён (non-fast-forward) — force push
                # Сервер — источник истины при деплое
                if "non-fast-forward" in (out + err) or "behind" in (out + err):
                    console.print(
                        "  [yellow]Remote имеет другие коммиты, "
                        "выполняю force push (сервер — источник истины)...[/yellow]"
                    )
                    rc, out, err = run_cmd(
                        f"cd {site_path} && git push -u origin main --force 2>&1"
                    )
                    if rc != 0:
                        return self._add_result(
                            13, "first_commit", "Первый коммит и пуш", False,
                            "Ошибка git push --force",
                            f"{out}\n{err}"[:300]
                        )
                    return self._add_result(
                        13, "first_commit", "Первый коммит и пуш", True,
                        f"Коммит: '{commit_msg}'",
                        "Force push выполнен (сервер — источник истины)"
                    )
                return self._add_result(
                    13, "first_commit", "Первый коммит и пуш", False,
                    "Ошибка git push", f"{out}\n{err}"[:300]
                )
            return self._add_result(
                13, "first_commit", "Первый коммит и пуш", True,
                f"Коммит: '{commit_msg}'", "Push выполнен"
            )
        else:
            return self._add_result(
                13, "first_commit", "Первый коммит и пуш", True,
                f"Коммит: '{commit_msg}'",
                "Push пропущен (нет репо/токена)"
            )

    # ── Блок 14: Финальные проверки (авто) ───────────────────────────────

    def execute_block14(self) -> BlockResult:
        checks = []
        all_ok = True
        project_name = self.ctx.get("project_name", "app")
        site_path = self.ctx.get("site_path", ".")

        rc, _, _ = run_cmd(f"pm2 describe {project_name} 2>/dev/null")
        if rc == 0:
            checks.append("PM2 процесс — запущен")
        else:
            checks.append("PM2 процесс — НЕ НАЙДЕН")
            all_ok = False

        if os.path.exists(os.path.join(site_path, ".env")):
            checks.append(".env — существует")
        else:
            checks.append(".env — НЕ НАЙДЕН")
            all_ok = False

        if os.path.exists(os.path.join(site_path, ".gitignore")):
            checks.append(".gitignore — существует")
        else:
            checks.append(".gitignore — НЕ НАЙДЕН")
            all_ok = False

        rc, out, _ = run_cmd(f"cd {site_path} && git remote -v")
        checks.append("Git remote — " + ("настроен" if "origin" in out else "НЕ НАСТРОЕН"))

        app_port = self.ctx.get("app_port", 0)
        checks.append(f"Порт: {app_port}" if app_port else "Порт — НЕ ВЫБРАН")

        # Проверяем HTTP-доступ через веб-сервер
        domain = self.ctx.get("site_domain", "") or self.ctx.get("domain", "")
        if domain:
            rc, out, _ = run_cmd(
                f"curl -s -o /dev/null -w '%{{http_code}}' "
                f"-H 'Host: {domain}' http://localhost:80 --max-time 5"
            )
            status = out.strip().replace("'", "")
            if status in ("200", "301", "302", "304"):
                checks.append(f"HTTP через веб-сервер — OK (статус {status})")
            elif status == "000":
                checks.append("HTTP через веб-сервер — НЕТ ОТВЕТА")
                all_ok = False
            else:
                checks.append(f"HTTP через веб-сервер — статус {status}")
                if status in ("502", "503"):
                    all_ok = False

        # Проверяем прямое подключение к приложению
        if app_port:
            rc, out, _ = run_cmd(
                f"curl -s -o /dev/null -w '%{{http_code}}' "
                f"http://localhost:{app_port}/ --max-time 3"
            )
            direct_status = out.strip().replace("'", "")
            if direct_status in ("200", "301", "302", "304"):
                checks.append(f"Прямой HTTP на порт {app_port} — OK")
            else:
                checks.append(f"Прямой HTTP на порт {app_port} — статус {direct_status}")
                all_ok = False

        msg = "Все проверки пройдены" if all_ok else "Есть проблемы"
        return self._add_result(14, "final_checks", "Финальные проверки", all_ok,
                                msg, '\n'.join(checks))

    # ── Блок 15: Автодеплой (авто) ───────────────────────────────────────

    def execute_block15(self) -> BlockResult:
        site_path = self.ctx.get("site_path", ".")
        project_name = self.ctx.get("project_name", "app")
        repo_name = self.ctx.get("repo_name", "")
        github_token = self.ctx.get("github_token", "")

        webhook_port = find_free_port(9000, 49000, self.used_ports)
        self.used_ports.add(webhook_port)

        secret = generate_password(24)

        webhook_script = f"""#!/usr/bin/env node
const http = require('http');
const {{ exec }} = require('child_process');

const PORT = {webhook_port};
const SECRET = '{secret}';
const PROJECT = '{project_name}';
const SITE_PATH = '{site_path}';

const server = http.createServer((req, res) => {{
  if (req.method === 'POST' && req.url === '/webhook') {{
    let body = '';
    req.on('data', chunk => {{ body += chunk; }});
    req.on('end', () => {{
      try {{
        const payload = JSON.parse(body);
        console.log(`[${{new Date().toISOString()}}] Webhook received for ${{payload.repository?.name || 'unknown'}}`);
        exec(`cd ${{SITE_PATH}} && git pull origin main && npm install && NODE_OPTIONS='--max-old-space-size=4096' npm run build && pm2 restart ${{PROJECT}}`,
          (error, stdout, stderr) => {{
            if (error) console.error('Deploy error:', error);
            else console.log('Deploy success:', stdout);
          }});
        res.writeHead(200, {{'Content-Type': 'application/json'}});
        res.end(JSON.stringify({{ status: 'ok', message: 'Deploy started' }}));
      }} catch(e) {{
        res.writeHead(400);
        res.end('Bad request');
      }}
    }});
  }} else {{
    res.writeHead(404);
    res.end('Not found');
  }}
}});

server.listen(PORT, '0.0.0.0', () => {{
  console.log(`Webhook server for ${{PROJECT}} listening on port ${{PORT}}`);
}});
"""
        webhook_path = os.path.join(site_path, "webhook-server.js")
        try:
            with open(webhook_path, 'w') as f:
                f.write(webhook_script)

            rc, out, err = run_cmd(
                f"cd {site_path} && pm2 start webhook-server.js "
                f"--name {project_name}-webhook"
            )
            run_cmd("pm2 save")

            if repo_name and github_token:
                run_cmd(
                    f"curl -s -X POST "
                    f"-H 'Authorization: token {github_token}' "
                    f"-H 'Accept: application/vnd.github.v3+json' "
                    f"https://api.github.com/repos/{repo_name}/hooks "
                    f"-d '{{\"name\":\"web\",\"active\":true,"
                    f"\"events\":[\"push\"],"
                    f"\"config\":{{\"url\":\"http://$(hostname -I | awk '{{print $1}}'):{webhook_port}/webhook\","
                    f"\"content_type\":\"json\"}}}}'"
                )

            self.ctx["webhook_port"] = webhook_port
            return self._add_result(15, "autodeploy", "Настройка автодеплоя", True,
                                    f"Webhook порт: {webhook_port}",
                                    f"Файл: {webhook_path}")
        except Exception as e:
            return self._add_result(15, "autodeploy", "Настройка автодеплоя", False,
                                    f"Ошибка: {e}")

    # ─── Маршрутизатор ──────────────────────────────────────────────────

    def execute_block(self, num: int) -> BlockResult:
        dispatch = {
            1: self.execute_block1,
            2: self.execute_block2,
            3: self.execute_block3,
            4: self.execute_block4,
            5: self.execute_block5,
            6: self.execute_block6,
            7: self.execute_block7,
            8: self.execute_block8,
            9: self.execute_block9,
            10: self.execute_block10,
            11: self.execute_block11,
            12: self.execute_block12,
            13: self.execute_block13,
            14: self.execute_block14,
            15: self.execute_block15,
        }
        handler = dispatch.get(num)
        if handler:
            return handler()
        return self._add_result(num, f"block{num}", f"Блок {num}", False,
                                "Неизвестный блок")

    def get_summary_text(self) -> str:
        """Текстовая сводка."""
        lines = []
        lines.append("=" * 60)
        lines.append("  СВОДКА РЕЗУЛЬТАТОВ АВТОДЕПЛОЯ")
        lines.append("=" * 60)
        lines.append("")

        ok = 0
        err = 0
        err_block = None

        for r in self.results:
            marker = "OK" if r.success else "ОШИБКА"
            lines.append(f"  {'✔' if r.success else '✘'} Блок {r.block_num:2d} | {r.title:35s} | {marker}")
            lines.append(f"         {r.message}")
            if r.details:
                for dl in r.details.split('\n'):
                    lines.append(f"           {dl}")
            lines.append("")
            if r.success:
                ok += 1
            else:
                err += 1
                if err_block is None:
                    err_block = r.block_num

        lines.append("-" * 60)
        lines.append(f"  Успешно: {ok}  |  Ошибки: {err}")
        if err_block:
            lines.append(f"  Первая ошибка в блоке: {err_block}")
        else:
            lines.append("  Все блоки выполнены успешно!")
        lines.append("=" * 60)
        return '\n'.join(lines)

    def save_log(self, base_dir: str = None):
        """Сохраняет лог в deploy-logs/."""
        if base_dir is None:
            base_dir = os.getcwd()
        log_dir = os.path.join(base_dir, "deploy-logs")
        os.makedirs(log_dir, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        project = self.ctx.get("project_name", "unknown")

        # .log
        log_file = os.path.join(log_dir, f"deploy_{project}_{timestamp}.log")
        with open(log_file, 'w', encoding='utf-8') as f:
            f.write(self.get_summary_text())
            f.write("\n\n--- КОНТЕКСТ ---\n")
            safe = {k: ("***" if ('password' in k.lower() or 'token' in k.lower()) and v else v)
                    for k, v in self.ctx.items() if not k.startswith('_')}
            f.write(json.dumps(safe, ensure_ascii=False, indent=2))

        # .json
        json_file = os.path.join(log_dir, f"deploy_{project}_{timestamp}.json")
        with open(json_file, 'w', encoding='utf-8') as f:
            json.dump({
                "timestamp": timestamp,
                "project": project,
                "results": [{"block": r.block_num, "title": r.title,
                             "success": r.success, "message": r.message}
                            for r in self.results],
                "context": safe,
            }, f, ensure_ascii=False, indent=2)

        return log_file


# ╔═══════════════════════════════════════════════════════════════════════════╗
# ║  ОПИСАНИЯ БЛОКОВ                                                         ║
# ╚═══════════════════════════════════════════════════════════════════════════╝

BLOCK_INFO = [
    (1,  "Название проекта и путь",       "MANUAL",  "Ввод данных"),
    (2,  "Выбор свободного порта",         "AUTO",    "Автоматически"),
    (3,  "Режим запуска (Standalone/Std)", "MANUAL",  "Выбор режима"),
    (4,  "Домен и SSL",                    "MANUAL",  "Ввод данных"),
    (5,  "Подготовка PM2 конфигурации",    "AUTO",    "Автоматически"),
    (6,  "Конфигурация веб-сервера",       "AUTO",    "Автоматически"),
    (7,  "Git-репозиторий",               "MANUAL",  "Ввод данных"),
    (8,  "Поиск файла базы данных",        "AUTO",    "Автоматически"),
    (9,  "Создание .gitignore",            "AUTO",    "Автоматически"),
    (10, "Создание .env",                  "AUTO",    "Автоматически"),
    (11, "Сборка и запуск",               "AUTO",    "npm install + next build + pm2 start"),
    (12, "Проверка репозитория и токена",  "CHECK",   "Авто + запрос при ошибке"),
    (13, "Первый коммит и пуш",            "AUTO",    "Автоматически"),
    (14, "Финальные проверки",             "AUTO",    "Автоматически"),
    (15, "Настройка автодеплоя",           "AUTO",    "Автоматически"),
]


# ╔═══════════════════════════════════════════════════════════════════════════╗
# ║  ОСНОВНОЙ ПОТОК                                                          ║
# ╚═══════════════════════════════════════════════════════════════════════════╝

def print_banner():
    console.print()
    console.print(Panel.fit(
        "[bold green]AutoDeploy[/bold green] — Автоматический деплой сайтов\n"
        "[dim]15 блоков. Большинство выполняются автоматически.[/dim]",
        border_style="green"
    ))
    console.print()


def print_block_header(num: int, title: str, block_type: str):
    """Красивый заголовок блока."""
    type_colors = {"AUTO": "cyan", "MANUAL": "yellow", "CHECK": "magenta"}
    color = type_colors.get(block_type, "white")
    console.print()
    console.print(Rule(
        f"[bold {color}]Блок {num}/15: {title}  [{block_type}][/bold {color}]",
        style=color
    ))


def print_result(result: BlockResult):
    """Вывод результата блока."""
    if result.success:
        console.print(f"  [bold green]  OK[/bold green]  {result.message}")
    else:
        console.print(f"  [bold red]ОШИБКА[/bold red]  {result.message}")
    if result.details:
        for line in result.details.split('\n'):
            console.print(f"         [dim]{line}[/dim]")


def print_final_summary(executor: DeployExecutor, ctx: Dict[str, Any]):
    """Финальная сводка."""
    console.print()
    console.print(Rule("[bold]СВОДКА РЕЗУЛЬТАТОВ[/bold]"))

    has_errors = any(not r.success for r in executor.results)
    if has_errors:
        console.print("[bold red]  Есть ошибки![/bold red]")
    else:
        console.print("[bold green]  Все блоки выполнены успешно![/bold green]")
    console.print()

    table = Table(box=box.ROUNDED, show_lines=False)
    table.add_column("#", style="bold", width=3)
    table.add_column("Блок", width=35)
    table.add_column("Статус", width=8)
    table.add_column("Результат", width=50)

    for r in executor.results:
        status = "[green]OK[/green]" if r.success else "[red]ОШИБКА[/red]"
        table.add_row(str(r.block_num), r.title, status, r.message)

    console.print(table)

    # Ключевые данные
    console.print()
    console.print("[bold]Ключевые параметры:[/bold]")
    key_data = [
        ("Проект", ctx.get("project_name", "")),
        ("Домен", ctx.get("domain", "")),
        ("Путь", ctx.get("site_path", "")),
        ("Порт приложения", str(ctx.get("app_port", ""))),
        ("Файл БД", ctx.get("db_file", "не найден")),
        ("Репозиторий", ctx.get("repo_name", "")),
        ("Webhook порт", str(ctx.get("webhook_port", ""))),
    ]
    for k, v in key_data:
        if v:
            console.print(f"  [cyan]{k}:[/cyan] {v}")

    # Лог
    log_file = executor.save_log()
    console.print(f"\n  [dim]Лог: {log_file}[/dim]")


def main():
    ctx: Dict[str, Any] = {}
    executor = DeployExecutor(ctx)

    print_banner()

    # ── Список блоков ────────────────────────────────────────────────────
    console.print("[bold]План выполнения:[/bold]")
    console.print()
    for num, title, btype, desc in BLOCK_INFO:
        type_colors = {"AUTO": "cyan", "MANUAL": "yellow", "CHECK": "magenta"}
        color = type_colors.get(btype, "white")
        console.print(f"  {num:2d}. [{color}]{btype:6s}[/{color}]  {title:35s}  [dim]{desc}[/dim]")
    console.print()
    console.print("[dim]Нажмите Enter для начала...[/dim]")
    input()

    # ── Блок 1: Проект и путь ────────────────────────────────────────────
    print_block_header(1, "Название проекта и путь", "MANUAL")
    console.print("Укажите название проекта и путь к директории сайта.")
    console.print("Путь — корневая директория сайта (например: /var/www/site.ru).")
    console.print()

    ctx["project_name"] = Prompt.ask("  [bold yellow]Название проекта[/bold yellow]")
    ctx["site_path"] = Prompt.ask("  [bold yellow]Путь к сайту[/bold yellow]",
                                  default=f"/var/www/{ctx.get('project_name', 'mysite')}")

    result = executor.execute_block(1)
    print_result(result)
    if not result.success:
        print_final_summary(executor, ctx)
        return

    # ── Блок 2: Свободный порт (авто) ────────────────────────────────────
    print_block_header(2, "Выбор свободного порта", "AUTO")
    console.print("Ищу свободный порт...")
    result = executor.execute_block(2)
    print_result(result)
    if not result.success:
        print_final_summary(executor, ctx)
        return

    # ── Блок 3: Режим запуска ─────────────────────────────────────────────
    print_block_header(3, "Режим запуска (Standalone / Standard)", "MANUAL")
    console.print("  [bold]Выберите режим запуска:[/bold]")
    console.print("  [green][1][/green] Standalone — запуск через [cyan]node .next/standalone/server.js[/cyan]")
    console.print("      (быстрее, меньше памяти, не требует node_modules для запуска)")
    console.print("      Требуется output: \"standalone\" в next.config.ts/js")
    console.print("  [green][2][/green] Standard — запуск через [cyan]npx next start -p PORT[/cyan]")
    console.print("      (требует полный node_modules, проще для отладки)")
    console.print("  [green][3][/green] Автоопределение после сборки")
    console.print()

    ctx["use_standalone"] = Prompt.ask("  [bold yellow]Ваш выбор[/bold yellow]",
                                       choices=["1", "2", "3"], default="3")

    result = executor.execute_block(3)
    print_result(result)
    if not result.success:
        print_final_summary(executor, ctx)
        return

    # ── Блок 4: Домен и SSL ─────────────────────────────────────────────
    print_block_header(4, "Домен и SSL", "MANUAL")
    ctx["site_domain"] = Prompt.ask("  [bold yellow]Домен[/bold yellow] (Enter чтобы пропустить)",
                                    default="")
    if ctx["site_domain"]:
        ctx["use_ssl"] = "yes" if Confirm.ask(
            "  [bold yellow]Настроить SSL (Let's Encrypt)?[/bold yellow]",
            default=False) else "no"
    else:
        ctx["use_ssl"] = "no"
        console.print("  [dim]Домен не указан — Apache не будет настроен[/dim]")

    result = executor.execute_block(4)
    print_result(result)
    if not result.success:
        print_final_summary(executor, ctx)
        return

    # ── Блоки 5-6: PM2 конфиг + Веб-сервер (авто) ────────────────────────
    for block_num in [5, 6]:
        _, title, btype, _ = BLOCK_INFO[block_num - 1]
        print_block_header(block_num, title, btype)
        console.print("Выполняется автоматически...")
        result = executor.execute_block(block_num)
        print_result(result)
        if not result.success:
            print_final_summary(executor, ctx)
            return

    # ── Блок 7: Репозиторий ──────────────────────────────────────────────
    print_block_header(7, "Git-репозиторий", "MANUAL")
    console.print("Укажите данные GitHub-репозитория.")
    console.print("Формат: владелец/имя (например: [cyan]MyUser/be2st.ru[/cyan])")
    console.print()

    ctx["repo_owner"] = Prompt.ask("  [bold yellow]Владелец репозитория[/bold yellow] (логин GitHub)")
    ctx["repo_short_name"] = Prompt.ask("  [bold yellow]Название репозитория[/bold yellow]")
    ctx["repo_name"] = f"{ctx['repo_owner']}/{ctx['repo_short_name']}".strip("/")
    ctx["repo_private"] = Confirm.ask("  [bold yellow]Репозиторий приватный?[/bold yellow]",
                                      default=True)
    ctx["github_token"] = Prompt.ask("  [bold yellow]GitHub Token[/bold yellow]",
                                     password=True)

    result = executor.execute_block(7)
    print_result(result)
    if not result.success:
        print_final_summary(executor, ctx)
        return

    # ── Блоки 8-10: Авто ──────────────────────────────────────────────────
    for block_num in [8, 9, 10]:
        _, title, btype, _ = BLOCK_INFO[block_num - 1]
        print_block_header(block_num, title, btype)
        console.print("Выполняется автоматически...")
        result = executor.execute_block(block_num)
        print_result(result)
        if not result.success:
            print_final_summary(executor, ctx)
            return

    # ── Блок 11: Сборка и запуск (отдельно — долгий процесс) ─────────────
    _, title, btype, _ = BLOCK_INFO[10]
    print_block_header(11, title, btype)
    console.print("[bold]Это может занять несколько минут...[/bold]")
    console.print()
    result = executor.execute_block(11)
    print_result(result)
    if not result.success:
        print_final_summary(executor, ctx)
        return

    # ── Блок 12: Проверка репо и токена (check) ──────────────────────────
    while True:
        print_block_header(12, "Проверка репозитория и токена", "CHECK")
        console.print("Проверяю доступ к репозиторию...")
        result = executor.execute_block(12)
        print_result(result)
        if result.success:
            break
        # Ошибка — запрашиваем исправления
        console.print()
        console.print("[bold red]Не удалось получить доступ к репозиторию.[/bold red]")
        console.print("Возможные причины: неверный токен, нет прав, репозиторий не найден.")
        console.print()
        console.print("  [green][1][/green] — Только токен")
        console.print("  [green][2][/green] — Владельца и имя репозитория")
        console.print("  [green][3][/green] — Всё (владелец, имя, токен)")
        retry_what = Prompt.ask(
            "  [bold yellow]Что исправить?[/bold yellow]",
            choices=["1", "2", "3"],
            default="1"
        )
        if retry_what == "1":
            ctx["github_token_retry"] = Prompt.ask("  [bold yellow]Новый GitHub Token[/bold yellow]",
                                                    password=True)
        elif retry_what == "2":
            ctx["repo_owner"] = Prompt.ask("  [bold yellow]Владелец репозитория[/bold yellow]",
                                           default=ctx.get("repo_owner", ""))
            ctx["repo_short_name"] = Prompt.ask("  [bold yellow]Название репозитория[/bold yellow]",
                                                default=ctx.get("repo_short_name", ""))
            ctx["repo_name"] = f"{ctx['repo_owner']}/{ctx['repo_short_name']}".strip("/")
        elif retry_what == "3":
            ctx["repo_owner"] = Prompt.ask("  [bold yellow]Владелец репозитория[/bold yellow]",
                                           default=ctx.get("repo_owner", ""))
            ctx["repo_short_name"] = Prompt.ask("  [bold yellow]Название репозитория[/bold yellow]",
                                                default=ctx.get("repo_short_name", ""))
            ctx["repo_name"] = f"{ctx['repo_owner']}/{ctx['repo_short_name']}".strip("/")
            ctx["github_token_retry"] = Prompt.ask("  [bold yellow]Новый GitHub Token[/bold yellow]",
                                                    password=True)
        # Удаляем старый неудачный результат чтобы не дублировать в сводке
        if executor.results and executor.results[-1].block_num == 12:
            executor.results.pop()

    # ── Блоки 13-15: Авто ────────────────────────────────────────────────
    for block_num in [13, 14, 15]:
        _, title, btype, _ = BLOCK_INFO[block_num - 1]
        print_block_header(block_num, title, btype)
        console.print("Выполняется автоматически...")
        result = executor.execute_block(block_num)
        print_result(result)
        if not result.success:
            break

    # ── Финальная сводка ─────────────────────────────────────────────────
    print_final_summary(executor, ctx)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print("\n\n[bold red]Прервано пользователем.[/bold red]")
        sys.exit(1)
