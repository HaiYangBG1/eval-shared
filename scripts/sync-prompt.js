#!/usr/bin/env node

/**
 * eval-sync-prompt
 * 同步 Langfuse Prompt 到本地（或从本地上传到 Langfuse）
 *
 * 用法：eval-sync-prompt --agent <agent-name> [--direction pull|push] [--label production]
 */

const fs = require('fs');
const path = require('path');

// js-yaml 可能在调用方的 node_modules 中（通过 promptfoo 间接安装）
let yaml;
try {
  yaml = require('js-yaml');
} catch {
  try {
    yaml = require(path.resolve(process.cwd(), 'node_modules', 'js-yaml'));
  } catch {
    console.error('❌ 找不到 js-yaml 模块。请确保项目中已安装 promptfoo（它包含 js-yaml）。');
    process.exit(1);
  }
}

// ── 解析命令行参数 ──
const args = process.argv.slice(2);
const agentIdx = args.indexOf('--agent');
const dirIdx = args.indexOf('--direction');
const labelIdx = args.indexOf('--label');

if (agentIdx === -1 || !args[agentIdx + 1]) {
  console.error('用法：eval-sync-prompt --agent <agent-name> [--direction pull|push] [--label production]');
  process.exit(1);
}

const agentName = args[agentIdx + 1];
const direction = dirIdx !== -1 ? args[dirIdx + 1] : 'pull';
const label = labelIdx !== -1 ? args[labelIdx + 1] : null;

// ── 环境变量检查 ──
// 兼容 LANGFUSE_HOST 和 LANGFUSE_BASE_URL 两种命名
const langfuseHost = process.env.LANGFUSE_HOST || process.env.LANGFUSE_BASE_URL;
if (!langfuseHost) {
  console.error('❌ 缺少环境变量：LANGFUSE_HOST 或 LANGFUSE_BASE_URL');
  process.exit(1);
}

const requiredEnvVars = ['LANGFUSE_PUBLIC_KEY', 'LANGFUSE_SECRET_KEY'];
for (const envVar of requiredEnvVars) {
  if (!process.env[envVar]) {
    console.error(`❌ 缺少环境变量：${envVar}`);
    process.exit(1);
  }
}

// ── 主逻辑 ──
async function main() {
  const { LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY } = process.env;
  const baseUrl = langfuseHost.replace(/\/$/, '');
  const authHeader = Buffer.from(`${LANGFUSE_PUBLIC_KEY}:${LANGFUSE_SECRET_KEY}`).toString('base64');

  const promptName = `${agentName}-prompt`;

  if (direction === 'pull') {
    console.log(`📥 拉取 Prompt：${promptName}`);

    try {
      const response = await fetch(`${baseUrl}/api/public/v2/prompts/${promptName}`, {
        headers: { Authorization: `Basic ${authHeader}` },
      });

      if (!response.ok) {
        throw new Error(`Langfuse API 返回 ${response.status}: ${response.statusText}`);
      }

      const prompt = await response.json();
      console.log(`✅ Prompt 拉取成功`);
      console.log(`   名称：${prompt.name}`);
      console.log(`   版本：${prompt.version}`);
      console.log(`   标签：${(prompt.labels || []).join(', ')}`);
    } catch (error) {
      console.error(`❌ 拉取失败：${error.message}`);
      process.exit(1);
    }
  } else if (direction === 'push') {
    console.log(`📤 上传 Prompt：${promptName}`);

    // 1. 定位 prompt.yaml 文件
    const promptFile = path.resolve(process.cwd(), 'agents', agentName, 'prompt.yaml');
    if (!fs.existsSync(promptFile)) {
      console.error(`❌ 文件不存在：${promptFile}`);
      process.exit(1);
    }

    // 2. 解析 YAML（chat message 格式）
    const promptContent = fs.readFileSync(promptFile, 'utf-8');
    const messages = yaml.load(promptContent);

    if (!Array.isArray(messages)) {
      console.error('❌ prompt.yaml 格式错误：期望数组（chat message 格式）');
      process.exit(1);
    }

    // 3. 构造 Langfuse API 请求体
    const body = {
      name: promptName,
      type: 'chat',
      prompt: messages,
    };

    // 可选：添加标签
    if (label) {
      body.labels = [label];
    }

    console.log(`   文件：${promptFile}`);
    console.log(`   消息数：${messages.length}`);
    console.log(`   标签：${label || '(无)'}`);

    // 4. POST 到 Langfuse
    try {
      const response = await fetch(`${baseUrl}/api/public/v2/prompts`, {
        method: 'POST',
        headers: {
          Authorization: `Basic ${authHeader}`,
          'Content-Type': 'application/json',
        },
        body: JSON.stringify(body),
      });

      if (!response.ok) {
        const errorBody = await response.text();
        throw new Error(`Langfuse API 返回 ${response.status}: ${errorBody}`);
      }

      const result = await response.json();
      console.log(`✅ Prompt 上传成功`);
      console.log(`   名称：${result.name}`);
      console.log(`   版本：${result.version}`);
      console.log(`   标签：${(result.labels || []).join(', ')}`);
    } catch (error) {
      console.error(`❌ 上传失败：${error.message}`);
      process.exit(1);
    }
  } else {
    console.error(`❌ 无效的 direction：${direction}，请使用 pull 或 push`);
    process.exit(1);
  }
}

main();
