"""Run the persona runtime: uvicorn dual_lobe_persona.api:app."""
import os

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "dual_lobe_persona.api:app",
        host=os.environ.get("DUAL_LOBE_PERSONA_HOST", "0.0.0.0"),
        port=int(os.environ.get("DUAL_LOBE_PERSONA_PORT", "8802")),
    )