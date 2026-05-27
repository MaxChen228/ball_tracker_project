using Godot;
using System;
using System.Collections.Generic;

// TrajectoryViewer
// ----------------
// Pulls a session's fitted ballistic trajectory from ball_tracker_project's
// `GET /sessions/{sid}/trajectory` endpoint and renders it as a 3D
// polyline plus an animated ball that flies along the fit curve.
//
// Wire (server-side, server world frame, X right / Y home→pitcher / Z up,
// gravity = (0, 0, -9.81)):
//
//     {
//       "session_id": "...", "algorithm_id": "...",
//       "frame": "server_world", "gravity": [0,0,-9.81],
//       "segments": [
//         {p0:[x,y,z], v0:[vx,vy,vz], t_anchor, t_start, t_end,
//          rmse_m, speed_kph}
//       ]
//     }
//
// Raw triangulated points are intentionally NOT on the wire — dashboard
// owns raw-point debug rendering. This viewer is fit-only.
//
// Frame transform (server world → Godot Y-up):
//
//     godot.x =  server.x
//     godot.y =  server.z          // Z up → Y up
//     godot.z = -server.y          // home→pitcher is +Y server, -Z Godot
//
// Gravity check: (0,0,-9.81) server → (0,-9.81,0) Godot ✓ ("down" in both).
public partial class TrajectoryViewer : Node3D
{
    [Export] public string ServerBaseUrl { get; set; } = "http://127.0.0.1:8765";
    [Export] public string DefaultSessionId { get; set; } = "";
    [Export] public string DefaultAlgorithmId { get; set; } = "ios_capture_time";

    // How many polyline samples per segment for the fit curve.
    [Export] public int SamplesPerSegment { get; set; } = 80;

    // Playback speed multiplier (1.0 = real time).
    [Export] public float PlaybackSpeed { get; set; } = 0.5f;

    private HttpRequest _http;
    private MeshInstance3D _polylineMesh;
    private ImmediateMesh _polyline;
    private MeshInstance3D _ball;
    private LineEdit _sessionInput;
    private Label _statusLabel;

    // Push channel — server pings us when a live session just finished
    // with non-empty segments. We treat the WS as a tap on the shoulder
    // and pull the actual trajectory via the same HTTP GET the manual
    // Load button uses.
    private WebSocketPeer _ws;
    private bool _wsConnected;
    private float _wsRetryDelay;     // simple linear back-off, capped

    private readonly List<SegmentFit> _segments = new();
    private float _tGlobalStart;
    private float _tGlobalEnd;
    private float _playTau;          // current absolute t (seconds)
    private bool _playing;

    private readonly struct SegmentFit
    {
        public readonly Vector3 P0;
        public readonly Vector3 V0;
        public readonly Vector3 G;
        public readonly float TAnchor;
        public readonly float TStart;
        public readonly float TEnd;
        public readonly float SpeedKph;

        public SegmentFit(Vector3 p0, Vector3 v0, Vector3 g,
                          float tAnchor, float tStart, float tEnd, float speedKph)
        {
            P0 = p0; V0 = v0; G = g;
            TAnchor = tAnchor; TStart = tStart; TEnd = tEnd; SpeedKph = speedKph;
        }

        public Vector3 Sample(float tAbs)
        {
            float tau = tAbs - TAnchor;
            return P0 + V0 * tau + 0.5f * G * tau * tau;
        }
    }

    public override void _Ready()
    {
        _http = new HttpRequest();
        AddChild(_http);
        _http.RequestCompleted += OnHttpRequestCompleted;

        _polyline = new ImmediateMesh();
        _polylineMesh = new MeshInstance3D
        {
            Mesh = _polyline,
            TopLevel = true,
        };
        var lineMat = new StandardMaterial3D
        {
            AlbedoColor = new Color(1.0f, 0.85f, 0.2f),
            ShadingMode = BaseMaterial3D.ShadingModeEnum.Unshaded,
            VertexColorUseAsAlbedo = false,
        };
        _polylineMesh.MaterialOverride = lineMat;
        AddChild(_polylineMesh);

        var ballMesh = new SphereMesh
        {
            Radius = 0.0365f,    // hardball radius ≈ 7.3 cm diameter
            Height = 0.073f,
        };
        _ball = new MeshInstance3D
        {
            Mesh = ballMesh,
            Visible = false,
        };
        var ballMat = new StandardMaterial3D
        {
            AlbedoColor = new Color(0.95f, 0.95f, 0.95f),
        };
        _ball.MaterialOverride = ballMat;
        AddChild(_ball);

        _sessionInput = GetNodeOrNull<LineEdit>("%SessionInput");
        _statusLabel = GetNodeOrNull<Label>("%StatusLabel");
        var loadBtn = GetNodeOrNull<Button>("%LoadButton");
        var playBtn = GetNodeOrNull<Button>("%PlayButton");
        if (_sessionInput != null && !string.IsNullOrEmpty(DefaultSessionId))
            _sessionInput.Text = DefaultSessionId;
        if (loadBtn != null) loadBtn.Pressed += OnLoadPressed;
        if (playBtn != null) playBtn.Pressed += OnPlayPressed;

        // Open the push channel. Failure is non-fatal — manual Load
        // still works. We retry inside _Process() so a server that
        // starts after the viewer recovers without an editor restart.
        _ws = new WebSocketPeer();
        TryConnectWebSocket();
    }

    public override void _Process(double delta)
    {
        PumpWebSocket((float)delta);

        if (!_playing || _segments.Count == 0) return;
        _playTau += (float)delta * PlaybackSpeed;
        if (_playTau > _tGlobalEnd)
            _playTau = _tGlobalStart;  // loop

        // Find segment whose [t_start, t_end] contains _playTau, else hide.
        Vector3? pos = null;
        foreach (var s in _segments)
        {
            if (_playTau >= s.TStart && _playTau <= s.TEnd)
            {
                pos = s.Sample(_playTau);
                break;
            }
        }
        if (pos.HasValue)
        {
            _ball.Visible = true;
            _ball.GlobalPosition = pos.Value;
        }
        else
        {
            _ball.Visible = false;
        }
    }

    private void OnLoadPressed()
    {
        if (_sessionInput == null) { SetStatus("[bootstrap] no SessionInput node"); return; }
        var sid = _sessionInput.Text.Trim();
        if (string.IsNullOrEmpty(sid)) { SetStatus("enter session id (s_xxx)"); return; }
        LoadSession(sid, DefaultAlgorithmId, "manual");
    }

    // Single load path — both the manual button and the WS push handler
    // funnel through here. Keep this the only place that calls
    // _http.Request() so the HTTP wire schema lives in one site.
    private void LoadSession(string sid, string algorithmId, string source)
    {
        if (_sessionInput != null) _sessionInput.Text = sid;
        var url = $"{ServerBaseUrl}/sessions/{sid}/trajectory?algorithm={algorithmId}";
        SetStatus($"[{source}] GET {url}");
        _http.CancelRequest();
        var err = _http.Request(url);
        if (err != Error.Ok) SetStatus($"http error: {err}");
    }

    private void OnPlayPressed()
    {
        if (_segments.Count == 0) return;
        _playing = !_playing;
        if (_playing) _playTau = _tGlobalStart;
    }

    private void OnHttpRequestCompleted(long result, long responseCode,
                                        string[] headers, byte[] body)
    {
        if (responseCode != 200)
        {
            SetStatus($"HTTP {responseCode}: {System.Text.Encoding.UTF8.GetString(body)}");
            return;
        }
        var json = Json.ParseString(System.Text.Encoding.UTF8.GetString(body));
        if (json.VariantType != Variant.Type.Dictionary)
        {
            SetStatus("malformed JSON (expected object)");
            return;
        }
        var doc = json.AsGodotDictionary();
        var frame = (string)doc.GetValueOrDefault("frame", "");
        if (frame != "server_world")
        {
            SetStatus($"unexpected frame={frame}; aborting render");
            return;
        }
        var gravityArr = doc["gravity"].AsGodotArray();
        Vector3 gServer = new(
            (float)(double)gravityArr[0],
            (float)(double)gravityArr[1],
            (float)(double)gravityArr[2]
        );
        var gGodot = ServerToGodot(gServer);

        _segments.Clear();
        var rawSegs = doc["segments"].AsGodotArray();
        if (rawSegs.Count == 0) { SetStatus("session has 0 segments"); RenderPolyline(); return; }

        float tStartMin = float.MaxValue, tEndMax = float.MinValue;
        foreach (var entry in rawSegs)
        {
            var s = entry.AsGodotDictionary();
            var p0 = ArrToVec3(s["p0"].AsGodotArray());
            var v0 = ArrToVec3(s["v0"].AsGodotArray());
            float tAnchor = (float)(double)s["t_anchor"];
            float tStart = (float)(double)s["t_start"];
            float tEnd = (float)(double)s["t_end"];
            float speedKph = (float)(double)s["speed_kph"];
            _segments.Add(new SegmentFit(
                ServerToGodot(p0), ServerToGodotDir(v0), gGodot,
                tAnchor, tStart, tEnd, speedKph));
            if (tStart < tStartMin) tStartMin = tStart;
            if (tEnd > tEndMax) tEndMax = tEnd;
        }
        _tGlobalStart = tStartMin;
        _tGlobalEnd = tEndMax;
        _playTau = tStartMin;
        SetStatus($"loaded {_segments.Count} segment(s); span {tEndMax - tStartMin:F2}s");
        RenderPolyline();
    }

    private void RenderPolyline()
    {
        _polyline.ClearSurfaces();
        if (_segments.Count == 0) return;
        _polyline.SurfaceBegin(Mesh.PrimitiveType.LineStrip);
        foreach (var s in _segments)
        {
            for (int i = 0; i < SamplesPerSegment; i++)
            {
                float t = Mathf.Lerp(s.TStart, s.TEnd, i / (float)(SamplesPerSegment - 1));
                _polyline.SurfaceAddVertex(s.Sample(t));
            }
        }
        _polyline.SurfaceEnd();
    }

    private static Vector3 ArrToVec3(Godot.Collections.Array arr) => new(
        (float)(double)arr[0], (float)(double)arr[1], (float)(double)arr[2]
    );

    // Server world (X right, Y home→pitcher, Z up) → Godot (X right, Y up,
    // -Z toward pitcher). See class-level docstring for gravity check.
    private static Vector3 ServerToGodot(Vector3 v) => new(v.X, v.Z, -v.Y);

    // Same rotation for direction vectors (no translation component).
    private static Vector3 ServerToGodotDir(Vector3 v) => new(v.X, v.Z, -v.Y);

    private void SetStatus(string msg)
    {
        GD.Print($"[TrajectoryViewer] {msg}");
        if (_statusLabel != null) _statusLabel.Text = msg;
    }

    // ----- WebSocket push channel -----

    private string WsUrl()
    {
        // ServerBaseUrl is "http(s)://host:port". Translate scheme,
        // append /sim/events.
        string scheme;
        string rest;
        if (ServerBaseUrl.StartsWith("https://"))
        {
            scheme = "wss://";
            rest = ServerBaseUrl.Substring("https://".Length);
        }
        else if (ServerBaseUrl.StartsWith("http://"))
        {
            scheme = "ws://";
            rest = ServerBaseUrl.Substring("http://".Length);
        }
        else
        {
            scheme = "ws://";
            rest = ServerBaseUrl;
        }
        return $"{scheme}{rest}/sim/events";
    }

    private void TryConnectWebSocket()
    {
        var url = WsUrl();
        GD.Print($"[TrajectoryViewer] WS connect → {url}");
        var err = _ws.ConnectToUrl(url);
        if (err != Error.Ok)
        {
            GD.PrintErr($"[TrajectoryViewer] WS connect error: {err}");
        }
        _wsConnected = false;
        _wsRetryDelay = 0f;
    }

    private void PumpWebSocket(float delta)
    {
        if (_ws == null) return;
        _ws.Poll();

        var rs = _ws.GetReadyState();
        switch (rs)
        {
            case WebSocketPeer.State.Open:
                if (!_wsConnected)
                {
                    _wsConnected = true;
                    GD.Print("[TrajectoryViewer] WS open");
                }
                while (_ws.GetAvailablePacketCount() > 0)
                {
                    var bytes = _ws.GetPacket();
                    var text = System.Text.Encoding.UTF8.GetString(bytes);
                    HandleWsMessage(text);
                }
                break;
            case WebSocketPeer.State.Closed:
                if (_wsConnected || _wsRetryDelay <= 0f)
                {
                    _wsConnected = false;
                    GD.Print("[TrajectoryViewer] WS closed; will retry");
                    _wsRetryDelay = 3.0f;  // seconds
                }
                _wsRetryDelay -= delta;
                if (_wsRetryDelay <= 0f) TryConnectWebSocket();
                break;
            // Connecting / Closing: just keep polling.
        }
    }

    private void HandleWsMessage(string text)
    {
        var parsed = Json.ParseString(text);
        if (parsed.VariantType != Variant.Type.Dictionary) return;
        var msg = parsed.AsGodotDictionary();
        if (!msg.ContainsKey("type")) return;
        var t = (string)msg["type"];
        switch (t)
        {
            case "hello":
                // Connection ack — no action.
                break;
            case "session_trajectory_ready":
                var sid = (string)msg.GetValueOrDefault("session_id", "");
                var algo = (string)msg.GetValueOrDefault("algorithm_id", DefaultAlgorithmId);
                var cause = (string)msg.GetValueOrDefault("cause", "push");
                if (string.IsNullOrEmpty(sid))
                {
                    GD.PrintErr("[TrajectoryViewer] push missing session_id");
                    return;
                }
                LoadSession(sid, algo, $"push:{cause}");
                break;
            default:
                // Tolerate unknown message types so the server can add
                // events without lock-stepping the Godot client.
                GD.Print($"[TrajectoryViewer] unknown WS msg type={t}");
                break;
        }
    }
}
