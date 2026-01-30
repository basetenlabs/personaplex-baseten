import { FC, MutableRefObject, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useSocket, SocketConfig } from "./hooks/useSocket";
import { SocketContext } from "./SocketContext";
import { ServerAudio } from "./components/ServerAudio/ServerAudio";
import { UserAudio } from "./components/UserAudio/UserAudio";
import { Button } from "../../components/Button/Button";
import { ServerAudioStats } from "./components/ServerAudio/ServerAudioStats";
import { AudioStats } from "./hooks/useServerAudio";
import { TextDisplay } from "./components/TextDisplay/TextDisplay";
import { MediaContext } from "./MediaContext";
import { ServerInfo } from "./components/ServerInfo/ServerInfo";
import { ModelParamsValues, useModelParams } from "./hooks/useModelParams";
import fixWebmDuration from "webm-duration-fix";
import { getMimeType, getExtension } from "./getMimeType";
import { type ThemeType } from "./hooks/useSystemTheme";

type ConversationProps = {
  workerAddr: string;
  workerAuthId?: string;
  sessionAuthId?: string;
  sessionId?: number;
  email?: string;
  theme: ThemeType;
  audioContext: MutableRefObject<AudioContext|null>;
  worklet: MutableRefObject<AudioWorkletNode|null>;
  onConversationEnd?: () => void;
  isBypass?: boolean;
  startConnection: () => Promise<void>;
} & Partial<ModelParamsValues>;


const buildURL = (workerAddr: string): string => {
  let resolvedAddr = workerAddr;
  if (workerAddr === "same" || workerAddr === "") {
    resolvedAddr = window.location.hostname + ":" + window.location.port;
    console.log("Overriding workerAddr to", resolvedAddr);
  }
  const wsProtocol = (window.location.protocol === 'https:') ? 'wss' : 'ws';
  const url = `${wsProtocol}://${resolvedAddr}/api/chat`;
  console.log("WebSocket URL:", url);
  return url;
};


export const Conversation:FC<ConversationProps> = ({
  workerAddr,
  workerAuthId,
  audioContext,
  worklet,
  sessionAuthId,
  sessionId,
  onConversationEnd,
  startConnection,
  isBypass=false,
  email,
  theme,
  ...params
}) => {
  const getAudioStats = useRef<() => AudioStats>(() => ({
    playedAudioDuration: 0,
    missedAudioDuration: 0,
    totalAudioMessages: 0,
    delay: 0,
    minPlaybackDelay: 0,
    maxPlaybackDelay: 0,
  }));
  const isRecording = useRef<boolean>(false);
  const audioChunks = useRef<Blob[]>([]);

  const audioStreamDestination = useRef<MediaStreamAudioDestinationNode>(audioContext.current!.createMediaStreamDestination());
  const stereoMerger = useRef<ChannelMergerNode>(audioContext.current!.createChannelMerger(2));
  const audioRecorder = useRef<MediaRecorder>(new MediaRecorder(audioStreamDestination.current.stream, { mimeType: getMimeType("audio"), audioBitsPerSecond: 128000  }));
  const [audioURL, setAudioURL] = useState<string>("");
  const [isOver, setIsOver] = useState(false);
  const modelParams = useModelParams(params);
  const micDuration = useRef<number>(0);
  const actualAudioPlayed = useRef<number>(0);
  const textContainerRef = useRef<HTMLDivElement>(null);
  const seed = useMemo(() => Math.round(1000000 * Math.random()), []);

  const WSURL = useMemo(() => buildURL(workerAddr), [workerAddr]);
  
  // Build config to send as first message
  const socketConfig: SocketConfig = useMemo(() => ({
    voice_prompt: modelParams.voicePrompt,
    text_prompt: modelParams.textPrompt,
    seed: seed,
  }), [modelParams.voicePrompt, modelParams.textPrompt, seed]);

  const onDisconnect = useCallback(() => {
    setIsOver(true);
    console.log("on disconnect!");
    stopRecording();
  }, [setIsOver]);

  const { socketStatus, sendMessage, socket, start, stop } = useSocket({
    // onMessage,
    uri: WSURL,
    config: socketConfig,
    onDisconnect,
  });
  useEffect(() => {
    audioRecorder.current.ondataavailable = (e) => {
      audioChunks.current.push(e.data);
    };
    audioRecorder.current.onstop = async () => {
      let blob: Blob;
      const mimeType = getMimeType("audio");
      if(mimeType.includes("webm")) {
        blob = await fixWebmDuration(new Blob(audioChunks.current, { type: mimeType }));
        } else {
          blob = new Blob(audioChunks.current, { type: mimeType });
      }
      setAudioURL(URL.createObjectURL(blob));
      audioChunks.current = [];
      console.log("Audio Recording and encoding finished");
    };
  }, [audioRecorder, setAudioURL, audioChunks]);


  useEffect(() => {
    start();
    return () => {
      stop();
    };
  }, [start, workerAuthId]);

  const startRecording = useCallback(() => {
    if(isRecording.current) {
      return;
    }
    console.log(Date.now() % 1000, "Starting recording");
    console.log("Starting recording");
    // Build stereo routing for recording: left = server (worklet), right = user mic (connected in useUserAudio)
    try {
      stereoMerger.current.disconnect();
    } catch {}
    try {
      worklet.current?.disconnect(audioStreamDestination.current);
    } catch {}
    // Route server audio (mono) to left channel of merger
    worklet.current?.connect(stereoMerger.current, 0, 0);
    // Connect merger to the MediaStream destination
    stereoMerger.current.connect(audioStreamDestination.current);

    setAudioURL("");
    audioRecorder.current.start();
    isRecording.current = true;
  }, [isRecording, worklet, audioStreamDestination, audioRecorder, stereoMerger]);

  const stopRecording = useCallback(() => {
    console.log("Stopping recording");
    console.log("isRecording", isRecording)
    if(!isRecording.current) {
      return;
    }
    try {
      worklet.current?.disconnect(stereoMerger.current);
    } catch {}
    try {
      stereoMerger.current.disconnect(audioStreamDestination.current);
    } catch {}
    audioRecorder.current.stop();
    isRecording.current = false;
  }, [isRecording, worklet, audioStreamDestination, audioRecorder, stereoMerger]);

  const onPressConnect = useCallback(async () => {
      if (isOver) {
        window.location.reload();
      } else {
        audioContext.current?.resume();
        if (socketStatus !== "connected") {
          start();
        } else {
          stop();
        }
      }
    }, [socketStatus, isOver, start, stop]);

  const socketColor = useMemo(() => {
    if (socketStatus === "connected") {
      return 'bg-[#76b900]';
    } else if (socketStatus === "connecting") {
      return 'bg-orange-300';
    } else {
      return 'bg-red-400';
    }
  }, [socketStatus]);

  const socketButtonMsg = useMemo(() => {
    if (isOver) {
      return 'New Conversation';
    }
    if (socketStatus === "connected") {
      return 'Disconnect';
    } else {
      return 'Connecting...';
    }
  }, [isOver, socketStatus]);

  return (
    <SocketContext.Provider
      value={{
        socketStatus,
        sendMessage,
        socket,
      }}
    >
    <div>
    <div className="main-grid h-screen max-h-screen w-screen p-4 max-w-96 md:max-w-screen-lg m-auto">
      <div className="controls text-center flex justify-center items-center gap-2">
         <Button
            onClick={onPressConnect}
            disabled={socketStatus !== "connected" && !isOver}
          >
            {socketButtonMsg}
          </Button>
          <div className={`h-4 w-4 rounded-full ${socketColor}`} />
        </div>
        {audioContext.current && worklet.current && <MediaContext.Provider value={
          {
            startRecording,
            stopRecording,
            audioContext: audioContext as MutableRefObject<AudioContext>,
            worklet: worklet as MutableRefObject<AudioWorkletNode>,
            audioStreamDestination,
            stereoMerger,
            micDuration,
            actualAudioPlayed,
          }
        }>
          <div className="relative player h-full max-h-full w-full justify-between gap-3 md:p-12">
              <ServerAudio
                setGetAudioStats={(callback: () => AudioStats) =>
                  (getAudioStats.current = callback)
                }
                theme={theme}
              />
              <UserAudio theme={theme}/>
              <div className="pt-8 text-sm flex justify-center items-center flex-col download-links">
                {audioURL && <div><a href={audioURL} download={`personaplex_audio.${getExtension("audio")}`} className="pt-2 text-center block">Download audio</a></div>}
              </div>
          </div>
          <div className="scrollbar player-text" ref={textContainerRef}>
            <TextDisplay containerRef={textContainerRef}/>
          </div>
          <div className="player-stats hidden md:block">
            <ServerAudioStats getAudioStats={getAudioStats} />
          </div></MediaContext.Provider>}
        </div>
        <div className="max-w-96 md:max-w-screen-lg p-4 m-auto text-center">
          <ServerInfo/>
        </div>
      </div>
    </SocketContext.Provider>
  );
};

        // </MediaContext.Provider> : undefined}
        // 
        // }></MediaContext.Provider>
