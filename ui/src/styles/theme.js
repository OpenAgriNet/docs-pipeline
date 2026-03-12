/**
 * Theme and style definitions for the document ingestion pipeline UI.
 *
 * These styles are currently duplicated in App.jsx.
 * After ShadCN migration, these will be replaced with Tailwind utilities.
 */

export const colors = {
  primary: '#4f46e5',
  success: '#10b981',
  danger: '#ef4444',
  warning: '#f59e0b',
  background: '#1a1a2e',
  text: '#374151',
  textLight: '#6b7280',
  border: '#d1d5db',
  borderLight: '#e5e7eb',
};

export const stageColors = {
  background: {
    registered: '#dbeafe',
    ocr_processing: '#fef3c7',
    ocr_review: '#fce7f3',
    translation_processing: '#fef3c7',
    translation_review: '#e0e7ff',
    chunking: '#fef3c7',
    chunk_review: '#fce7f3',
    ready_for_ingestion: '#d1fae5',
    ingesting: '#fef3c7',
    completed: '#d1fae5',
    failed: '#fee2e2',
  },
  text: {
    registered: '#1e40af',
    ocr_processing: '#92400e',
    ocr_review: '#9d174d',
    translation_processing: '#92400e',
    translation_review: '#3730a3',
    chunking: '#92400e',
    chunk_review: '#9d174d',
    ready_for_ingestion: '#065f46',
    ingesting: '#92400e',
    completed: '#065f46',
    failed: '#991b1b',
  },
};

export const stepperStatus = {
  completed: {
    background: colors.success,
    color: 'white',
    textColor: '#065f46',
  },
  active: {
    background: colors.primary,
    color: 'white',
    textColor: colors.primary,
    border: '3px solid #c7d2fe',
  },
  failed: {
    background: colors.danger,
    color: 'white',
    textColor: colors.danger,
  },
  pending: {
    background: colors.borderLight,
    color: colors.textLight,
    textColor: colors.textLight,
  },
};

export const spacing = {
  xs: '4px',
  sm: '8px',
  md: '12px',
  lg: '16px',
  xl: '20px',
  xxl: '24px',
};

export const breakpoints = {
  sm: '640px',
  md: '768px',
  lg: '1024px',
  xl: '1280px',
};
