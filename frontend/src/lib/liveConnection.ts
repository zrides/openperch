// Browser side of live view: a receive-only WebRTC connection to the hub's relay.
// The SDP offer travels through the authenticated hub API (non-trickle ICE).

import { api } from './api';

export interface LiveConnection {
  stream: MediaStream;
  close(): void;
}

function gatheringComplete(pc: RTCPeerConnection, timeoutMs: number): Promise<void> {
  if (pc.iceGatheringState === 'complete') return Promise.resolve();
  return new Promise((resolve) => {
    const done = () => {
      pc.removeEventListener('icegatheringstatechange', check);
      clearTimeout(timer);
      resolve();
    };
    const check = () => { if (pc.iceGatheringState === 'complete') done(); };
    // Host candidates arrive almost immediately; don't wait on slow interfaces.
    const timer = setTimeout(done, timeoutMs);
    pc.addEventListener('icegatheringstatechange', check);
  });
}

export async function connectLive(onState: (state: RTCPeerConnectionState) => void): Promise<LiveConnection> {
  const pc = new RTCPeerConnection();
  const stream = new MediaStream();
  pc.addTransceiver('video', { direction: 'recvonly' });
  pc.ontrack = (event) => stream.addTrack(event.track);
  pc.onconnectionstatechange = () => onState(pc.connectionState);
  try {
    await pc.setLocalDescription(await pc.createOffer());
    await gatheringComplete(pc, 2000);
    const { sdp } = await api.post<{ sdp: string }>('/api/live/offer', { sdp: pc.localDescription?.sdp });
    await pc.setRemoteDescription({ type: 'answer', sdp });
  } catch (error) {
    pc.close();
    throw error;
  }
  return {
    stream,
    close() {
      pc.onconnectionstatechange = null;
      pc.close();
      stream.getTracks().forEach((track) => track.stop());
    },
  };
}
