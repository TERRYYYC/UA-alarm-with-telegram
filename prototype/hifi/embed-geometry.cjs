// Deterministic build step: inline static SVG geometry into the standalone deliverable.
const fs=require('node:fs'),path=require('node:path');
const file=path.join(__dirname,'situation-monitor.html');
const svg=fs.readFileSync(path.join(__dirname,'geography.svg.fragment'),'utf8');
if(/<script|<foreignObject|\bon\w+\s*=|(?:href|src)\s*=/i.test(svg))throw new Error('Unexpected active or external SVG content');
const html=fs.readFileSync(file,'utf8');
if(!html.includes('<!-- GEOGRAPHY_START -->'))throw new Error('Missing geometry insertion boundary');
fs.writeFileSync(file,html.replace(/<!-- GEOGRAPHY_START -->[\s\S]*?<!-- GEOGRAPHY_END -->/,'<!-- GEOGRAPHY_START -->\n'+svg+'\n<!-- GEOGRAPHY_END -->'));
console.log('Embedded static geography; standalone HTML needs no network.');
