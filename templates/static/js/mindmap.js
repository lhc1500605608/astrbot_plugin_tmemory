/* mindmap.js — D3.js 思维导图渲染（依赖 d3 全局变量） */

const TYPE_COLORS = { preference:'#58a6ff', fact:'#3fb950', task:'#d29922', restriction:'#f85149', style:'#bc8cff' };
let svgZoom, svgG;

function renderMindmap(memories, userId) {
  const svg = d3.select('#mindmapSvg');
  svg.selectAll('*').remove();
  const container = document.getElementById('panelMindmap');
  const W = container.clientWidth || 800;
  const H = container.clientHeight || 500;
  svg.attr('viewBox', `0 0 ${W} ${H}`);

  if (!memories || memories.length === 0) {
    svg.append('text').attr('x', W/2).attr('y', H/2).attr('text-anchor','middle')
      .attr('fill', 'var(--text2)').attr('font-size', 14).text('选择一个用户以查看记忆导图');
    return;
  }

  const grouped = {};
  memories.forEach(m => {
    const t = m.memory_type || 'fact';
    if (!grouped[t]) grouped[t] = [];
    grouped[t].push(m);
  });

  const root = {
    name: userId || '用户',
    children: Object.entries(grouped).map(([type, items]) => ({
      name: type, type,
      children: items.map(m => ({
        name: m.memory.length > 30 ? m.memory.slice(0, 30) + '…' : m.memory,
        fullText: m.memory, type, memData: m, importance: m.importance || 0.5
      }))
    }))
  };

  const hierarchy = d3.hierarchy(root);
  d3.tree().size([H - 80, W - 260])(hierarchy);
  svgG = svg.append('g').attr('transform', 'translate(80, 40)');
  svgZoom = d3.zoom().scaleExtent([0.3, 3]).on('zoom', e => svgG.attr('transform', e.transform));
  svg.call(svgZoom).call(svgZoom.transform, d3.zoomIdentity.translate(80, 40));

  svgG.selectAll('.link').data(hierarchy.links()).join('path').attr('class','link')
    .attr('d', d3.linkHorizontal().x(d => d.y).y(d => d.x))
    .attr('stroke', d => TYPE_COLORS[d.target.data.type] || 'var(--border)').attr('stroke-opacity', 0.4);

  const nodes = svgG.selectAll('.node').data(hierarchy.descendants()).join('g').attr('class','node')
    .attr('transform', d => `translate(${d.y},${d.x})`);
  nodes.append('circle')
    .attr('r', d => d.depth === 0 ? 14 : d.depth === 1 ? 10 : 5 + (d.data.importance || 0.5) * 6)
    .attr('fill', d => d.depth === 0 ? 'var(--accent)' : TYPE_COLORS[d.data.type] || '#666')
    .attr('stroke', d => d.depth === 0 ? '#fff' : TYPE_COLORS[d.data.type] || '#666')
    .attr('fill-opacity', d => d.depth === 2 ? 0.7 : 1)
    .on('click', (e, d) => { if (d.data.memData) openEditModal(d.data.memData); })
    .append('title').text(d => d.data.fullText || d.data.name);
  nodes.append('text')
    .attr('dy', d => d.depth === 0 ? -20 : d.children ? -16 : 4)
    .attr('x', d => d.children ? 0 : 12)
    .attr('text-anchor', d => d.children ? 'middle' : 'start')
    .attr('font-weight', d => d.depth <= 1 ? 600 : 400)
    .attr('font-size', d => d.depth === 0 ? 14 : d.depth === 1 ? 13 : 11)
    .text(d => d.data.name);
}

function zoomIn() { d3.select('#mindmapSvg').transition().duration(300).call(svgZoom.scaleBy, 1.3); }
function zoomOut() { d3.select('#mindmapSvg').transition().duration(300).call(svgZoom.scaleBy, 0.7); }
function resetZoom() { d3.select('#mindmapSvg').transition().duration(300).call(svgZoom.transform, d3.zoomIdentity.translate(80, 40)); }
