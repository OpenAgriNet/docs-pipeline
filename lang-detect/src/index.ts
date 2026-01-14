import express, { Request, Response, NextFunction } from 'express';
import os from 'os';
const franc = require('franc-min');

const app = express();
const PORT = process.env.PORT || 3000;
const HOSTNAME = os.hostname();

// Request logging middleware
app.use((req: Request, res: Response, next: NextFunction) => {
  const start = Date.now();
  res.on('finish', () => {
    const duration = Date.now() - start;
    if (req.path !== '/health') {
      console.log(`[${HOSTNAME}] ${req.method} ${req.path} - ${res.statusCode} - ${duration}ms`);
    }
  });
  next();
});

app.use(express.json({ limit: '10mb' }));

// ISO 639-3 to ISO 639-1 mapping for Indian languages
const iso3to1: Record<string, string> = {
  eng: 'en',
  hin: 'hi',
  guj: 'gu',
  mar: 'mr',
  tam: 'ta',
  tel: 'te',
  kan: 'kn',
  mal: 'ml',
  pan: 'pa',
  ben: 'bn',
  ori: 'or',
  urd: 'ur',
  san: 'sa',
  nep: 'ne',
  und: 'unknown'
};

function detectLanguage(text: string): string {
  if (!text || text.trim().length < 10) {
    return 'en'; // Default to English for very short text
  }

  const detected = franc(text);
  return iso3to1[detected] || detected || 'unknown';
}

interface DetectRequest {
  text: string;
}

interface DetectBatchRequest {
  texts: string[];
}

// Health check endpoint
app.get('/health', (_req: Request, res: Response) => {
  res.json({ status: 'ok' });
});

// Single text language detection
app.post('/detect', (req: Request<{}, {}, DetectRequest>, res: Response) => {
  try {
    const { text } = req.body;

    if (!text || typeof text !== 'string') {
      res.status(400).json({ error: 'text field is required and must be a string' });
      return;
    }

    const language = detectLanguage(text);
    res.json({
      language,
      text_preview: text.substring(0, 100)
    });
  } catch (error) {
    console.error('Detection error:', error);
    res.status(500).json({ error: 'Language detection failed' });
  }
});

// Batch language detection
app.post('/detect/batch', (req: Request<{}, {}, DetectBatchRequest>, res: Response) => {
  try {
    const { texts } = req.body;

    if (!texts || !Array.isArray(texts)) {
      res.status(400).json({ error: 'texts field is required and must be an array' });
      return;
    }

    console.log(`[${HOSTNAME}] Batch request: ${texts.length} texts`);
    const results = texts.map((text, index) => {
      try {
        const language = detectLanguage(text);
        return {
          index,
          language,
          text_preview: text.substring(0, 100)
        };
      } catch (err) {
        return {
          index,
          language: 'unknown',
          error: 'Detection failed for this text'
        };
      }
    });

    res.json({ results });
  } catch (error) {
    console.error('Batch detection error:', error);
    res.status(500).json({ error: 'Batch language detection failed' });
  }
});

app.listen(PORT, () => {
  console.log(`Language detection service running on port ${PORT}`);
});
