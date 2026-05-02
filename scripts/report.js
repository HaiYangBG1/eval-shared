#!/usr/bin/env node

/**
 * eval-report
 * 读取 PromptFoo 评估输出，生成 Markdown 格式的摘要报告
 *
 * 用法：eval-report [--input <output.json>] [--output <report.md>] [--agent <agent-name>]
 */

const fs = require('fs');
const path = require('path');

// ── 解析命令行参数 ──
const args = process.argv.slice(2);
const inputIdx = args.indexOf('--input');
const outputIdx = args.indexOf('--output');
const agentIdx = args.indexOf('--agent');

const agentName = agentIdx !== -1 ? args[agentIdx + 1] : null;
const inputPath = inputIdx !== -1
  ? args[inputIdx + 1]
  : agentName
    ? `agents/${agentName}/output/latest.json`
    : 'output/latest.json';
const outputPath = outputIdx !== -1
  ? args[outputIdx + 1]
  : inputPath.replace(/\.json$/, '-report.md');

// ── 主逻辑 ──
function main() {
  if (!fs.existsSync(inputPath)) {
    // 尝试从 .promptfoo 缓存中找最新结果
    const cacheDir = '.promptfoo';
    if (fs.existsSync(cacheDir)) {
      console.error(`❌ 找不到评估输出文件：${inputPath}`);
      console.error('💡 提示：先运行 promptfoo eval -o output/latest.json 生成输出文件');
    } else {
      console.error(`❌ 找不到评估输出文件：${inputPath}`);
    }
    process.exit(1);
  }

  const raw = JSON.parse(fs.readFileSync(inputPath, 'utf-8'));
  const results = raw.results || raw;
  const evalResults = Array.isArray(results) ? results : results.results || [];

  // ── 统计数据 ──
  let totalTests = 0;
  let passCount = 0;
  let failCount = 0;
  let errorCount = 0;
  const failedCases = [];

  for (const result of evalResults) {
    totalTests++;
    const success = result.success ?? result.pass;
    if (success === true) {
      passCount++;
    } else if (success === false) {
      failCount++;
      failedCases.push({
        vars: result.vars || {},
        output: (result.response?.output || result.output || '').slice(0, 200),
        failReasons: (result.gradingResult?.componentResults || result.assertionResults || [])
          .filter((r) => !r.pass)
          .map((r) => r.reason || r.assertion?.value || 'Unknown')
          .slice(0, 3),
      });
    } else {
      errorCount++;
    }
  }

  const passRate = totalTests > 0 ? ((passCount / totalTests) * 100).toFixed(1) : '0.0';

  // ── 生成报告 ──
  const now = new Date().toISOString().replace('T', ' ').slice(0, 19);
  const lines = [
    `# 📊 评估报告`,
    '',
    `> 生成时间：${now}`,
    agentName ? `> Agent：${agentName}` : '',
    `> 数据来源：\`${inputPath}\``,
    '',
    '## 摘要',
    '',
    `| 指标 | 值 |`,
    `|------|-----|`,
    `| 总测试数 | ${totalTests} |`,
    `| ✅ 通过 | ${passCount} |`,
    `| ❌ 失败 | ${failCount} |`,
    `| ⚠️ 错误 | ${errorCount} |`,
    `| **通过率** | **${passRate}%** |`,
    '',
  ];

  // 通过率颜色提示
  const rate = parseFloat(passRate);
  if (rate >= 95) {
    lines.push('> 🟢 优秀：通过率 ≥ 95%');
  } else if (rate >= 80) {
    lines.push('> 🟡 良好：通过率 80% - 95%，部分用例需要关注');
  } else {
    lines.push('> 🔴 需改进：通过率 < 80%，建议排查失败用例');
  }
  lines.push('');

  // 失败用例详情
  if (failedCases.length > 0) {
    lines.push('## 失败用例');
    lines.push('');
    const showCount = Math.min(failedCases.length, 10);
    for (let i = 0; i < showCount; i++) {
      const c = failedCases[i];
      const input = typeof c.vars === 'object'
        ? JSON.stringify(c.vars, null, 2).slice(0, 150)
        : String(c.vars).slice(0, 150);
      lines.push(`### ${i + 1}. 失败用例`);
      lines.push('');
      lines.push('**输入：**');
      lines.push('```json');
      lines.push(input);
      lines.push('```');
      lines.push('');
      lines.push(`**输出（截取）：** ${c.output}...`);
      lines.push('');
      if (c.failReasons.length > 0) {
        lines.push('**失败原因：**');
        for (const reason of c.failReasons) {
          lines.push(`- ${reason.slice(0, 200)}`);
        }
      }
      lines.push('');
      lines.push('---');
      lines.push('');
    }
    if (failedCases.length > showCount) {
      lines.push(`> ℹ️ 还有 ${failedCases.length - showCount} 个失败用例未列出，请查看原始输出文件。`);
      lines.push('');
    }
  }

  // 写入文件
  const dir = path.dirname(outputPath);
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(outputPath, lines.join('\n'));

  console.log(`📊 报告已生成 → ${outputPath}`);
  console.log(`   通过率：${passRate}%  (${passCount}/${totalTests})`);
  if (failCount > 0) {
    console.log(`   ❌ ${failCount} 个用例失败，详情见报告。`);
  }
}

main();
