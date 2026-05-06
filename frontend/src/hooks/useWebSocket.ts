import { useEffect, useRef, useCallback } from 'react';

export function useWebSocket(channel: string, onMessage: (data: any) => void) {
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectRef = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);

  const connect = useCallback(() => {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    // Dev: through Vite proxy; Prod: direct to backend port 8000
    const host = import.meta.env.DEV
      ? window.location.host
      : `${window.location.hostname}:8000`;
    const wsUrl = `${protocol}//${host}/ws/${channel}`;

    const ws = new WebSocket(wsUrl);
    wsRef.current = ws;

    ws.onopen = () => console.log(`WS ${channel} connected`);
    ws.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        onMessage(data);
      } catch {}
    };
    ws.onclose = () => {
      reconnectRef.current = setTimeout(connect, 3000);
    };
    ws.onerror = () => ws.close();
  }, [channel, onMessage]);

  useEffect(() => {
    connect();
    return () => {
      clearTimeout(reconnectRef.current);
      wsRef.current?.close();
    };
  }, [connect]);

  return wsRef;
}
