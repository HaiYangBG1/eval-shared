#!/usr/bin/env node

/**
 * eval-export-dspy
 * 将 Langfuse Dataset 导出为 DSPy Example 格式
 *
 * 用法：eval-export-dspy --agent <agent-name> [--output <output-path>]
 */

const fs = require('fs');
const path = require('path');

// ── 解析命令行参数 ──
const args = process.argv.slice(2);
const agentIdx = args.indexOf('--agent');
const outputIdx = args.indexOf('--output');

if (agentIdx === -1 || !args[agentIdx + 1]) {
  console.error('用法：eval-export-dspy --agent <agent-name> [--output <output-path>]');
  process.exit(1);
}

const agentName = args[agentIdx + 1];
const outputPath = outputIdx !== -1 ? args[outputIdx + 1] : `output/${agentName}-dspy-examples.json`;

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

  console.log(`📦 从 Langfuse 导出数据集：${agentName}`);

  try {
    // 1. 获取数据集条目
    const response = await fetch(`${baseUrl}/api/public/v2/dataset-items?datasetName=${agentName}`, {
      headers: { Authorization: `Basic ${authHeader}` },
    });

    if (!response.ok) {
      throw new Error(`Langfuse API 返回 ${response.status}: ${response.statusText}`);
    }

    const { data: items } = await response.json();

    // 2. 转换为 DSPy Example 格式
    // DSPy Example 格式：{ "input_field": "value", "output_field": "value" }
    const examples = items.map((item) => ({
      // 输入字段
      query: item.input?.query || item.input?.question || JSON.stringify(item.input),
      // 输出字段
      answer: item.expectedOutput
        ? typeof item.expectedOutput === 'string'
          ? item.expectedOutput
          : JSON.stringify(item.expectedOutput)
        : '',
      // 元数据
      _metadata: {
        source: 'langfuse',
        datasetItemId: item.id,
        exportedAt: new Date().toISOString(),
      },
    }));

    // 3. 写入文件
    const dir = path.dirname(outputPath);
    fs.mkdirSync(dir, { recursive: true });
    fs.writeFileSync(outputPath, JSON.stringify(examples, null, 2));

    console.log(`✅ 导出完成！${examples.length} 条 Example → ${outputPath}`);
    console.log('');
    console.log('在 DSPy 中使用：');
    console.log('  import dspy, json');
    console.log(`  with open("${outputPath}") as f:`);
    console.log('      data = json.load(f)');
    console.log('  examples = [dspy.Example(query=d["query"], answer=d["answer"]).with_inputs("query") for d in data]');
  } catch (error) {
    console.error(`❌ 导出失败：${error.message}`);
    process.exit(1);
  }
}

main();
