import asyncio
import json
import random
import time
from pathlib import Path

import chess
import chess.engine
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
async def index():
    return FileResponse("static/index.html")


def format_score_white(score: chess.engine.PovScore) -> str:
    """Format score from white's perspective. + = white advantage."""
    white_score = score.white()
    if white_score.is_mate():
        mate_in = white_score.mate()
        return f"M{mate_in}"
    cp = white_score.score()
    if cp is None:
        return "0.00"
    return f"{cp / 100:+.2f}"


def format_score_pov(score: chess.engine.PovScore, side: int) -> str:
    """Format score from the engine's perspective (side: 0=white, 1=black)."""
    pov = score.white() if side == 0 else score.black()
    if pov.is_mate():
        return f"M{pov.mate()}"
    cp = pov.score()
    if cp is None:
        return "0.00"
    return f"{cp / 100:+.2f}"


def pv_to_san(board: chess.Board, pv: list[chess.Move], max_moves: int = 8) -> str:
    """Convert a PV (list of moves) to SAN notation."""
    sans = []
    b = board.copy()
    for move in pv[:max_moves]:
        if move in b.legal_moves:
            sans.append(b.san(move))
            b.push(move)
        else:
            break
    return " ".join(sans)


# Fair openings book — each entry is (name, [moves in UCI notation])
# Selected to be well-known and roughly equal according to engine analysis.
OPENINGS = [
    # Sicilian: sharp, tactical, opposite-side castling potential
    ("Sicilian Najdorf 6.Bg5", ["e2e4", "c7c5", "g1f3", "d7d6", "d2d4", "c5d4", "f3d4", "g8f6", "b1c3", "a7a6", "c1g5"]),
    ("Sicilian Dragon Yugoslav Attack", ["e2e4", "c7c5", "g1f3", "d7d6", "d2d4", "c5d4", "f3d4", "g8f6", "b1c3", "g7g6", "c1e3", "f8g7", "f2f3"]),
    ("Sicilian Sveshnikov", ["e2e4", "c7c5", "g1f3", "b8c6", "d2d4", "c5d4", "f3d4", "g8f6", "b1c3", "e7e5", "d4b5", "d7d6"]),
    ("Sicilian Taimanov English Attack", ["e2e4", "c7c5", "g1f3", "e7e6", "d2d4", "c5d4", "f3d4", "b8c6", "b1c3", "d8c7", "c1e3", "a7a6", "f2f3"]),
    # King pawn: open tactical games
    ("Italian Evans Gambit", ["e2e4", "e7e5", "g1f3", "b8c6", "f1c4", "f8c5", "b2b4", "c5b4", "c2c3", "b4a5"]),
    ("Ruy Lopez Marshall Attack", ["e2e4", "e7e5", "g1f3", "b8c6", "f1b5", "a7a6", "b5a4", "g8f6", "e1g1", "f8e7", "f1e1", "b7b5", "a4b3", "e8g8", "c2c3", "d7d5"]),
    ("Scotch Gambit", ["e2e4", "e7e5", "g1f3", "b8c6", "d2d4", "e5d4", "f1c4", "f8c5", "c2c3"]),
    ("King's Gambit Accepted", ["e2e4", "e7e5", "f2f4", "e5f4", "g1f3", "g7g5", "f1c4", "f8g7"]),
    ("Vienna Gambit", ["e2e4", "e7e5", "b1c3", "g8f6", "f2f4", "d7d5", "f4e5", "f6e4"]),
    # French/Caro: sharp main lines
    ("French Poisoned Pawn", ["e2e4", "e7e6", "d2d4", "d7d5", "b1c3", "f8b4", "e4e5", "c7c5", "a2a3", "b4c3", "b2c3", "b8c6"]),
    ("Caro-Kann Tal Variation", ["e2e4", "c7c6", "d2d4", "d7d5", "b1c3", "d5e4", "c3e4", "g8f6", "e4f6", "e7f6"]),
    # Queen pawn: dynamic, imbalanced structures
    ("King's Indian Mar del Plata", ["d2d4", "g8f6", "c2c4", "g7g6", "b1c3", "f8g7", "e2e4", "d7d6", "g1f3", "e8g8", "f1e2", "e7e5", "e1g1", "b8c6", "d4d5", "c6e7"]),
    ("Grunfeld Exchange", ["d2d4", "g8f6", "c2c4", "g7g6", "b1c3", "d7d5", "c4d5", "f6d5", "e2e4", "d5c3", "b2c3", "f8g7", "f1c4"]),
    ("Benoni Modern Main Line", ["d2d4", "g8f6", "c2c4", "c7c5", "d4d5", "e7e6", "b1c3", "e6d5", "c4d5", "d7d6", "e2e4", "g7g6", "f1d3"]),
    ("Benko Gambit Accepted", ["d2d4", "g8f6", "c2c4", "c7c5", "d4d5", "b7b5", "c4b5", "a7a6", "b5a6"]),
    ("Semi-Slav Botvinnik", ["d2d4", "d7d5", "c2c4", "c7c6", "g1f3", "g8f6", "b1c3", "e7e6", "c1g5", "d5c4", "e2e4"]),
    ("Nimzo-Indian Saemisch", ["d2d4", "g8f6", "c2c4", "e7e6", "b1c3", "f8b4", "a2a3", "b4c3", "b2c3", "c7c5"]),
    ("Dutch Leningrad", ["d2d4", "f7f5", "g2g3", "g8f6", "f1g2", "g7g6", "g1f3", "f8g7", "e1g1", "e8g8", "c2c4", "d7d6"]),
    # Gambits and wild lines
    ("Morra Gambit Accepted", ["e2e4", "c7c5", "d2d4", "c5d4", "c2c3", "d4c3", "b1c3", "b8c6", "g1f3", "d7d6", "f1c4"]),
    ("Albin Counter-Gambit", ["d2d4", "d7d5", "c2c4", "e7e5", "d4e5", "d5d4", "g1f3", "b8c6"]),
]


class Match:
    def __init__(self, engine1_path: str, engine2_path: str, base_time: float, increment: float, ws: WebSocket, threads: int = 2, hash_mb: int = 1024, opening: tuple[str, list[str]] | None = None):
        self.engine1_path = engine1_path
        self.engine2_path = engine2_path
        self.base_time = base_time
        self.increment = increment
        self.ws = ws
        self.threads = threads
        self.hash_mb = hash_mb
        self.board = chess.Board()
        self.clocks = [base_time, base_time]  # [white, black]
        self.moves: list[str] = []
        self.fens: list[str] = [self.board.fen()]  # FEN after each half-move (index 0 = start)
        self.move_ucis: list[str] = []  # UCI string for each half-move
        self.running = False
        self.opening_name = ""
        self.engine_names = ["Engine 1", "Engine 2"]  # [white, black]

        # Apply opening moves
        if opening:
            self.opening_name = opening[0]
            for uci_move in opening[1]:
                move = chess.Move.from_uci(uci_move)
                if move in self.board.legal_moves:
                    self.moves.append(self.board.san(move))
                    self.move_ucis.append(move.uci())
                    self.board.push(move)
                    self.fens.append(self.board.fen())
        # Latest engine info per side: [white_info, black_info]
        self.engine_info = [
            {"eval": "0.00", "eval_pov": "0.00", "pv": "", "depth": 0, "wdl": None, "nps": None, "hashfull": None, "pv_first_uci": None},
            {"eval": "0.00", "eval_pov": "0.00", "pv": "", "depth": 0, "wdl": None, "nps": None, "hashfull": None, "pv_first_uci": None},
        ]

    async def send_state(self, result: str | None = None):
        # Get last move in UCI for arrow drawing
        last_move_uci = None
        if self.board.move_stack:
            last = self.board.move_stack[-1]
            last_move_uci = last.uci()
        state = {
            "type": "state",
            "fen": self.board.fen(),
            "fens": self.fens,
            "moves": self.moves,
            "moveUcis": self.move_ucis,
            "wtime": round(self.clocks[0], 3),
            "btime": round(self.clocks[1], 3),
            "result": result,
            "lastMove": self.moves[-1] if self.moves else None,
            "lastMoveUci": last_move_uci,
            "turn": "white" if self.board.turn == chess.WHITE else "black",
            "engine_info": self.engine_info,
            "clock_ts": time.time(),
            "opening": self.opening_name,
            "engine_names": self.engine_names,
        }
        await self.ws.send_json(state)

    async def send_thinking(self, side: int):
        """Send a thinking update with a formatted PV line for the history."""
        info = self.engine_info[side]
        # Build a formatted line: "d24  +0.35  1.2Mnps  hash:45%  e4 e5 Nf3 ..."
        parts = [f"d{info['depth']}"]
        parts.append(info["eval"])
        if info["wdl"]:
            w, d, l = info["wdl"]
            total = w + d + l
            if total > 0:
                parts.append(f"W:{w*100//total}% D:{d*100//total}% L:{l*100//total}%")
        if info["nps"] is not None:
            parts.append(f"{info['nps'] / 1_000_000:.1f}Mnps")
        if info["hashfull"] is not None:
            parts.append(f"hash:{info['hashfull'] / 10:.0f}%")
        parts.append(info["pv"])
        pv_line = "  ".join(parts)

        msg = {
            "type": "thinking",
            "side": side,
            "eval": info["eval"],
            "eval_pov": info["eval_pov"],
            "pv": info["pv"],
            "depth": info["depth"],
            "wdl": info["wdl"],
            "nps": info["nps"],
            "hashfull": info["hashfull"],
            "pv_line": pv_line,
            "pv_first_uci": info["pv_first_uci"],
        }
        await self.ws.send_json(msg)

    async def run(self):
        self.running = True
        engine1 = None
        engine2 = None
        try:
            transport1, engine1 = await chess.engine.popen_uci(self.engine1_path)
            transport2, engine2 = await chess.engine.popen_uci(self.engine2_path)

            # Grab engine names from UCI id
            self.engine_names[0] = engine1.id.get("name", "Engine 1")
            self.engine_names[1] = engine2.id.get("name", "Engine 2")

            for eng in [engine1, engine2]:
                opts = {"Threads": self.threads, "Hash": self.hash_mb}
                if "UCI_ShowWDL" in eng.options:
                    opts["UCI_ShowWDL"] = True
                try:
                    await eng.configure(opts)
                except chess.engine.EngineError:
                    pass

            engines = [engine1, engine2]  # [white, black]

            await self.send_state()

            while self.running and not self.board.is_game_over():
                side = 0 if self.board.turn == chess.WHITE else 1
                engine = engines[side]

                # Signal frontend to clear PV history for this side
                # Send the white-normalized eval from this engine's last search as baseline
                await self.ws.send_json({
                    "type": "new_move",
                    "side": side,
                    "prev_eval": self.engine_info[side]["eval"],
                })

                wtime = max(self.clocks[0], 0.01)
                btime = max(self.clocks[1], 0.01)
                limit = chess.engine.Limit(
                    white_clock=wtime,
                    black_clock=btime,
                    white_inc=self.increment,
                    black_inc=self.increment,
                )

                start = time.monotonic()
                best_move = None
                last_depth_sent = -1
                try:
                    with await engine.analysis(self.board, limit) as analysis:
                        async for info in analysis:
                            if not self.running:
                                break
                            if "score" in info:
                                self.engine_info[side]["eval"] = format_score_white(info["score"])
                                self.engine_info[side]["eval_pov"] = format_score_pov(info["score"], side)
                            if "pv" in info and len(info["pv"]) > 0:
                                self.engine_info[side]["pv"] = pv_to_san(self.board, info["pv"])
                                self.engine_info[side]["pv_first_uci"] = info["pv"][0].uci()
                            if "depth" in info:
                                self.engine_info[side]["depth"] = info["depth"]
                            if "wdl" in info:
                                wdl = info["wdl"]
                                # Keep from engine's POV (relative)
                                if hasattr(wdl, 'relative'):
                                    r = wdl.relative
                                    self.engine_info[side]["wdl"] = [r.wins, r.draws, r.losses]
                                elif hasattr(wdl, 'white'):
                                    # Fallback: convert to engine's POV
                                    if side == 0:
                                        w = wdl.white()
                                    else:
                                        w = wdl.black()
                                    self.engine_info[side]["wdl"] = [w.wins, w.draws, w.losses]
                                else:
                                    self.engine_info[side]["wdl"] = list(wdl)
                            if "nps" in info:
                                self.engine_info[side]["nps"] = info["nps"]
                            if "hashfull" in info:
                                self.engine_info[side]["hashfull"] = info["hashfull"]
                            # Only send one update per depth (main PV)
                            cur_depth = self.engine_info[side]["depth"]
                            if cur_depth > last_depth_sent and "pv" in info and "score" in info:
                                last_depth_sent = cur_depth
                                try:
                                    await self.send_thinking(side)
                                except Exception:
                                    pass
                            if best_move is not None:
                                break
                        best_move = analysis.info.get("pv", [None])[0] if "pv" in analysis.info else None
                        if best_move is None and hasattr(analysis, 'best'):
                            best_move = analysis.best.move if analysis.best else None
                except chess.engine.EngineTerminatedError:
                    await self.send_state(result="Engine crashed")
                    return

                elapsed = time.monotonic() - start
                self.clocks[side] -= elapsed
                self.clocks[side] += self.increment

                if self.clocks[side] <= 0:
                    self.clocks[side] = 0
                    winner = "Black" if side == 0 else "White"
                    await self.send_state(result=f"{winner} wins on time")
                    return

                if best_move is None or best_move not in self.board.legal_moves:
                    winner = "Black" if side == 0 else "White"
                    await self.send_state(result=f"{winner} wins - illegal move")
                    return

                san = self.board.san(best_move)
                self.move_ucis.append(best_move.uci())
                self.board.push(best_move)
                self.moves.append(san)
                self.fens.append(self.board.fen())
                await self.send_state()

                # Small delay so the frontend can animate
                await asyncio.sleep(0.05)

            # Game over by board rules
            if self.board.is_game_over():
                outcome = self.board.outcome()
                if outcome is None:
                    result_str = "Game over"
                elif outcome.winner is None:
                    result_str = f"Draw - {outcome.termination.name}"
                elif outcome.winner == chess.WHITE:
                    result_str = f"White wins - {outcome.termination.name}"
                else:
                    result_str = f"Black wins - {outcome.termination.name}"
                await self.send_state(result=result_str)

        except Exception as e:
            try:
                await self.ws.send_json({"type": "error", "message": str(e)})
            except Exception:
                pass
        finally:
            self.running = False
            if engine1:
                await engine1.quit()
            if engine2:
                await engine2.quit()

    def stop(self):
        self.running = False


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    match: Match | None = None
    match_task: asyncio.Task | None = None

    try:
        while True:
            data = await ws.receive_text()
            msg = json.loads(data)
            action = msg.get("action")

            if action == "start":
                if match:
                    match.stop()
                if match_task and not match_task.done():
                    match_task.cancel()
                    try:
                        await match_task
                    except (asyncio.CancelledError, Exception):
                        pass

                engine1 = msg["engine1"]
                engine2 = msg["engine2"]
                tc = msg.get("time", "3+0")
                parts = tc.split("+")
                base_time = float(parts[0]) * 60
                increment = float(parts[1]) if len(parts) > 1 else 0.0

                threads = int(msg.get("threads", 2))
                hash_mb = int(msg.get("hash", 1024))

                opening = random.choice(OPENINGS)

                match = Match(engine1, engine2, base_time, increment, ws, threads, hash_mb, opening)
                match_task = asyncio.create_task(match.run())

            elif action == "stop":
                if match:
                    match.stop()

    except WebSocketDisconnect:
        if match:
            match.stop()
    except Exception:
        if match:
            match.stop()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
