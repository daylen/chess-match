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


def format_score(score: chess.engine.PovScore, board: chess.Board) -> str:
    """Format score from white's perspective. + = white advantage."""
    white_score = score.white()
    if white_score.is_mate():
        mate_in = white_score.mate()
        return f"M{mate_in}" if mate_in > 0 else f"M{mate_in}"
    cp = white_score.score()
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
    ("Sicilian Najdorf", ["e2e4", "c7c5", "g1f3", "d7d6", "d2d4", "c5d4", "f3d4", "g8f6", "b1c3", "a7a6"]),
    ("Sicilian Dragon", ["e2e4", "c7c5", "g1f3", "d7d6", "d2d4", "c5d4", "f3d4", "g8f6", "b1c3", "g7g6"]),
    ("Ruy Lopez Berlin", ["e2e4", "e7e5", "g1f3", "b8c6", "f1b5", "g8f6", "d2d3"]),
    ("Ruy Lopez Marshall", ["e2e4", "e7e5", "g1f3", "b8c6", "f1b5", "a7a6", "b5a4", "g8f6", "e1g1", "f8e7"]),
    ("Italian Game Giuoco Piano", ["e2e4", "e7e5", "g1f3", "b8c6", "f1c4", "f8c5", "c2c3", "g8f6", "d2d4"]),
    ("Queen's Gambit Declined", ["d2d4", "d7d5", "c2c4", "e7e6", "b1c3", "g8f6", "c4d5", "e6d5", "f1g5"]),
    ("Slav Defense", ["d2d4", "d7d5", "c2c4", "c7c6", "g1f3", "g8f6", "b1c3", "d5c4"]),
    ("King's Indian Classical", ["d2d4", "g8f6", "c2c4", "g7g6", "b1c3", "f8g7", "e2e4", "d7d6", "g1f3", "e8g8"]),
    ("Nimzo-Indian Defense", ["d2d4", "g8f6", "c2c4", "e7e6", "b1c3", "f8b4", "d1c2"]),
    ("Grunfeld Defense", ["d2d4", "g8f6", "c2c4", "g7g6", "b1c3", "d7d5", "c4d5", "f6d5"]),
    ("Catalan Opening", ["d2d4", "g8f6", "c2c4", "e7e6", "g2g3", "d7d5", "f1g2", "f8e7"]),
    ("English Opening Symmetrical", ["c2c4", "c7c5", "b1c3", "b8c6", "g1f3", "g8f6", "g2g3"]),
    ("Caro-Kann Classical", ["e2e4", "c7c6", "d2d4", "d7d5", "b1c3", "d5e4", "c3e4", "c8f5"]),
    ("French Defense Winawer", ["e2e4", "e7e6", "d2d4", "d7d5", "b1c3", "f8b4", "e4e5", "c7c5"]),
    ("Petroff Defense", ["e2e4", "e7e5", "g1f3", "g8f6", "f3e5", "d7d6", "e5f3", "f6e4"]),
    ("Semi-Slav Meran", ["d2d4", "d7d5", "c2c4", "c7c6", "g1f3", "g8f6", "b1c3", "e7e6", "e2e3"]),
    ("Ragozin Defense", ["d2d4", "d7d5", "c2c4", "e7e6", "b1c3", "g8f6", "g1f3", "f8b4"]),
    ("Scotch Game", ["e2e4", "e7e5", "g1f3", "b8c6", "d2d4", "e5d4", "f3d4", "f8c5"]),
    ("Queen's Indian Defense", ["d2d4", "g8f6", "c2c4", "e7e6", "g1f3", "b7b6", "g2g3", "c8b7"]),
    ("Benoni Defense", ["d2d4", "g8f6", "c2c4", "c7c5", "d4d5", "e7e6", "b1c3", "e6d5", "c4d5", "d7d6"]),
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
        self.running = False
        self.opening_name = ""

        # Apply opening moves
        if opening:
            self.opening_name = opening[0]
            for uci_move in opening[1]:
                move = chess.Move.from_uci(uci_move)
                if move in self.board.legal_moves:
                    self.moves.append(self.board.san(move))
                    self.board.push(move)
        # Latest engine info per side: [white_info, black_info]
        self.engine_info = [
            {"eval": "0.00", "pv": "", "depth": 0, "wdl": None},
            {"eval": "0.00", "pv": "", "depth": 0, "wdl": None},
        ]

    async def send_state(self, result: str | None = None, thinking_side: int | None = None):
        state = {
            "type": "state",
            "fen": self.board.fen(),
            "moves": self.moves,
            "wtime": round(self.clocks[0], 3),
            "btime": round(self.clocks[1], 3),
            "result": result,
            "lastMove": self.moves[-1] if self.moves else None,
            "turn": "white" if self.board.turn == chess.WHITE else "black",
            "engine_info": self.engine_info,
            # Timestamp so frontend knows when clocks were snapshotted
            "clock_ts": time.time(),
            "thinking_side": thinking_side,
            "opening": self.opening_name,
        }
        await self.ws.send_json(state)

    async def send_thinking(self, side: int):
        """Send a lightweight thinking update."""
        msg = {
            "type": "thinking",
            "side": side,
            "eval": self.engine_info[side]["eval"],
            "pv": self.engine_info[side]["pv"],
            "depth": self.engine_info[side]["depth"],
            "wdl": self.engine_info[side]["wdl"],
        }
        await self.ws.send_json(msg)

    async def run(self):
        self.running = True
        engine1 = None
        engine2 = None
        try:
            transport1, engine1 = await chess.engine.popen_uci(self.engine1_path)
            transport2, engine2 = await chess.engine.popen_uci(self.engine2_path)

            for eng in [engine1, engine2]:
                opts = {"Threads": self.threads, "Hash": self.hash_mb}
                # Enable WDL output if the engine supports it
                if "UCI_ShowWDL" in eng.options:
                    opts["UCI_ShowWDL"] = True
                try:
                    await eng.configure(opts)
                except chess.engine.EngineError:
                    pass  # Engine may not support these options

            engines = [engine1, engine2]  # [white, black]

            await self.send_state()

            while self.running and not self.board.is_game_over():
                side = 0 if self.board.turn == chess.WHITE else 1
                engine = engines[side]

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
                try:
                    with await engine.analysis(self.board, limit) as analysis:
                        async for info in analysis:
                            if not self.running:
                                break
                            # Extract eval and PV from info
                            if "score" in info:
                                self.engine_info[side]["eval"] = format_score(info["score"], self.board)
                            if "pv" in info and len(info["pv"]) > 0:
                                self.engine_info[side]["pv"] = pv_to_san(self.board, info["pv"])
                            if "depth" in info:
                                self.engine_info[side]["depth"] = info["depth"]
                            if "wdl" in info:
                                # WDL from engine's POV — normalize to white's POV
                                wdl = info["wdl"]
                                if hasattr(wdl, 'white'):
                                    w = wdl.white()
                                    self.engine_info[side]["wdl"] = [w.wins, w.draws, w.losses]
                                else:
                                    self.engine_info[side]["wdl"] = list(wdl)
                            # Send thinking updates periodically
                            try:
                                await self.send_thinking(side)
                            except Exception:
                                pass
                            # Check if this is the final bestmove
                            if best_move is not None:
                                break
                        # analysis context manager waits for bestmove
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
                self.board.push(best_move)
                self.moves.append(san)
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
                # Stop any existing match
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
