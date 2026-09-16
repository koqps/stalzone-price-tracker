/** @type {import('next').NextConfig} */
const API_ORIGIN = process.env.API_ORIGIN || 'https://stalzone-tracker.onrender.com';
const nextConfig = {
  images: { unoptimized: true },
  async rewrites(){ return [{ source:'/api/:path*', destination:`${API_ORIGIN}/api/:path*` }] }
};
export default nextConfig;
