#!/usr/bin/env node

/**
 * eval-sync-dataset
 * 从 Langfuse Dataset 同步数据到本地 YAML 文件
 *
 * 用法：eval-sync-dataset --agent <agent-name> [--dataset <dataset-name>]
 */

const { execSync } = require('child_process');
const fs = require('fs');
const path = require('path');
const yaml = require('yaml'); // 需要 npm install yaml

// ── 解析命令行参数 ──
const args = process.argv.slice(2);
const agentIdx = args.indexOf('--agent');
const datasetIdx = args.indexOf('--dataset');

if (agentIdx === -1 || !args[agentIdx + 1]) {
  console.error('用法：eval-sync-dataset --agent <agent-name> [--dataset <dataset-name>]');
  process.exit(1);
}

const agentName = args[agentIdx + 1];
const datasetName = datasetIdx !== -1 ? args[datasetIdx + 1] : agentName;

// ── 环境变量检查 ──
const requiredEnvVars = ['LANGFUSE_PUBLIC_KEY', 'LANGFUSE_SECRET_KEY', 'LANGFUSE_HOST'];
for (const envVar of requiredEnvVars) {
  if (!process.env[envVar]) {
    console.error(`❌ 缺少环境变量：${envVar}`);
    console.error('请确保 .env 文件已配置。');
    process.exit(1);
  }
}

// ── 主逻辑 ──
async function main() {
  const { LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, LANGFUSE_HOST } = process.env;
  const baseUrl = LANGFUSE_HOST.replace(/\/$/, '');

  console.log(`📦 正在从 Langfuse 同步数据集：${datasetName}`);
  console.log(`📁 目标 Agent：${agentName}`);

  try {
    // 1. 获取数据集
    const authHeader = Buffer.from(`${LANGFUSE_PUBLIC_KEY}:${LANGFUSE_SECRET_KEY}`).toString('base64');
    const response = await fetch(`${baseUrl}/api/public/v2/datasets/${datasetName}`, {
      headers: { Authorization: `Basic ${authHeader}` },
    });

    if (!response.ok) {
      throw new Error(`Langfuse API 返回 ${response.status}: ${response.statusText}`);
    }

    const dataset = await response.json();

    // 2. 获取数据集条目
    const itemsResponse = await fetch(`${baseUrl}/api/public/v2/dataset-items?datasetName=${datasetName}`, {
      headers: { Authorization: `Basic ${authHeader}` },
    });

    if (!itemsResponse.ok) {
      throw new Error(`获取数据集条目失败：${itemsResponse.status}`);
    }

    const { data: items } = await itemsResponse.json();

    // 3. 转换为 PromptFoo 测试格式
    const tests = items.map((item) => ({
      vars: item.input || {},
      assert: item.expectedOutput
        ? [{ type: 'llm-rubric', value: `期望输出应接近：${JSON.stringify(item.expectedOutput)}` }]
        : [],
    }));

    // 4. 写入本地文件
    const outputDir = path.join('agents', agentName, 'datasets');
    fs.mkdirSync(outputDir, { recursive: true });

    const outputPath = path.join(outputDir, 'golden.yaml');
    const header = [
      '# 黄金测试集：从 Langfuse Dataset 自动同步',
      `# 来源数据集：${datasetName}`,
      `# 同步时间：${new Date().toISOString()}`,
      `# 条目数量：${tests.length}`,
      '',
    ].join('\n');

    fs.writeFileSync(outputPath, header + yaml.stringify(tests));

    console.log(`✅ 同步完成！${tests.length} 条测试数据 → ${outputPath}`);
  } catch (error) {
    console.error(`❌ 同步失败：${error.message}`);
    process.exit(1);
  }
}

main();
