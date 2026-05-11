const fs = require('fs');
const path = require('path');

// Cargador manual de .env blindado
const envPath = path.join(__dirname, '.env');
let envConfig = {};
if (fs.existsSync(envPath)) {
  const content = fs.readFileSync(envPath, 'utf8');
  content.split('\n').forEach(line => {
    const trimmedLine = line.trim();
    if (trimmedLine && !trimmedLine.startsWith('#') && trimmedLine.includes('=')) {
      const [key, ...val] = trimmedLine.split('=');
      envConfig[key.trim()] = val.join('=').trim();
    }
  });
}

// Limpieza de URL para Zammad MCP
const baseZammadUrl = (envConfig.ZAMMAD_URL || "https://serviunix-helpdesk.zammad.com").replace(/\/$/, "");
const mcpZammadUrl = baseZammadUrl.includes("/api/v1") ? baseZammadUrl : baseZammadUrl + "/api/v1";

module.exports = {
  apps: [
    {
      name: "zammad-mcp",
      script: "C:\\Users\\Serviunix\\.local\\bin\\uv.exe",
      args: ["tool", "run", "--from", "git+https://github.com/basher83/zammad-mcp.git", "mcp-zammad"],
      env: {
        ...envConfig,
        ZAMMAD_URL: mcpZammadUrl,
        MCP_TRANSPORT: "http",
        MCP_HOST: "127.0.0.1",
        MCP_PORT: "8001"
      },
      autorestart: true,
      restart_delay: 5000,
      min_uptime: "10s"
    },
    {
      name: "fastapi",
      script: "C:\\Users\\Serviunix\\Documents\\support_ia\\venv\\Scripts\\python.exe",
      args: ["run_prod.py"],
      cwd: "C:\\Users\\Serviunix\\Documents\\support_ia",
      env: {
        ...envConfig
      },
      delay: 20000,
      restart_delay: 5000,
      min_uptime: "10s"
    }
  ]
};
