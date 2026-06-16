"""
Module: FFmpeg Processor
Purpose: Handles media file manipulation using FFmpeg. Responsible for extracting 
         audio, converting it to 16kHz Mono format (optimal for Whisper), and 
         splitting large files into smaller chunks for parallel processing.
"""