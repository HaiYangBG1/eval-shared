#!/usr/bin/env node

/**
 * eval-sync-prompt
 * 同步 Langfuse Prompt 到本地（或从本地上传到 Langfuse）
 *
 * 用法：eval-sync-prompt --agent <agent-name> [--direction pull|push]
 */

const fs = require('fs');
const path = require('path');

// ── 解析命令行参数 ──
const args = process.argv.slice(2);
const agentIdx = args.indexOf('--agent');
const dirIdx = args.indexOf('--direction');

if (agentIdx === -1 || !args[agentIdx + 1]) {
  console.error('用法：eval-sync-prompt --agent <agent-name> [--direction pull|push]');
  process.exit(1);
}

const agentName = args[agentIdx + 1];
const direction = dirIdx !== -1 ? args[dirIdx + 1] : 'pull';

// ── 环境变量检查 ──
const requiredEnvVars = ['LANGFUSE_PUBLIC_KEY', 'LANGFUSE_SECRET_KEY', 'LANGFUSE_HOST'];
for (const envVar of requiredEnvVars) {
  if (!process.env[envVar]) {
    console.error(`❌ 缺少环境变量：${envVar}`);
    process.exit(1);
  }
}

// ── 主逻辑 ──
async function main() {
  const { LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, LANGFUSE_HOST } = process.env;
  const baseUrl = LANGFUSE_HOST.replace(/\/$/, '');
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
    // TODO: 从本地文件读取 Prompt 内容并上传
    console.log('⚠️  push 功能待实现');
  } else {
    console.error(`❌ 无效的 direction：${direction}，请使用 pull 或 push`);
    process.exit(1);
  }
}

main();
