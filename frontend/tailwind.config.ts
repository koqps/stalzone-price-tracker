import type { Config } from 'tailwindcss';
const config: Config = {
  content: ['./app/**/*.{ts,tsx}','./components/**/*.{ts,tsx}'],
  theme: { extend: { colors: { ink:'#0b0e14',navy:'#111622',card:'#161c28',line:'#222b3d',accent:'#5b8def' }, boxShadow:{panel:'0 18px 60px rgba(0,0,0,.28)'} } },
  plugins: []
};
export default config;
