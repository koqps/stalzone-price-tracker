'use client';
import {QueryClient,QueryClientProvider} from '@tanstack/react-query';import {useState} from 'react';
export default function Providers({children}:{children:React.ReactNode}){const [client]=useState(()=>new QueryClient({defaultOptions:{queries:{staleTime:15000,refetchInterval:15000,refetchOnWindowFocus:true}}}));return <QueryClientProvider client={client}>{children}</QueryClientProvider>}
