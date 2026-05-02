#!/usr/bin/env node

/**
 * eval-promote
 * 将 Langfuse Prompt 从 staging 提升为 production
 *
 * 用法：eval-promote --agent <agent-name> [--dry-run]
 */

const args = process.argv.slice(2);
const agentIdx = args.indexOf('--agent');
const dryRun = args.includes('--dry-run');

if (agentIdx === -1 || !args[agentIdx + 1]) {
  console.error('用法：eval-promote --agent <agent-name> [--dry-run]');
  process.exit(1);
}

const agentName = args[agentIdx + 1];

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

  console.log(`🚀 提升 Prompt：${promptName}`);
  console.log(`   staging → production`);

  if (dryRun) {
    console.log('⚠️  Dry-run 模式，不会实际执行变更。');
  }

  try {
    // 1. 获取当前 staging 版本
    const response = await fetch(`${baseUrl}/api/public/v2/prompts/${promptName}?label=staging`, {
      headers: { Authorization: `Basic ${authHeader}` },
    });

    if (!response.ok) {
      throw new Error(`获取 staging Prompt 失败：${response.status}`);
    }

    const prompt = await response.json();
    console.log(`   当前 staging 版本：${prompt.version}`);

    if (dryRun) {
      console.log('✅ Dry-run 完成，以上版本将被标记为 production。');
      return;
    }

    // 2. 将 staging 版本标记为 production
    // Langfuse API: PATCH /api/public/v2/prompts/{promptName}/versions/{version}
    // TODO: 根据 Langfuse API 版本调整具体接口
    console.log('⚠️  自动标记 production 功能需根据 Langfuse API 版本适配。');
    console.log('   请手动在 Langfuse UI 中将上述版本标记为 production。');
  } catch (error) {
    console.error(`❌ 提升失败：${error.message}`);
    process.exit(1);
  }
}

main();
