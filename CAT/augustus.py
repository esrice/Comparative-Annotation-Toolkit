"""
Runs AugustusTM(R) on the input transMap records.

This program takes as input a genePred of transMap output, an attributes table describing the biotype and
gene-transcript relationships, and an optional RNAseq hints database and runs Augustus on each transcript.

Transcripts are evaluated for the following features to help decide how they are converted into hints:

    1) OriginalIntrons. If any new gaps are not within a wiggle distance (in transcript space) of original introns,
        do not provide them as hints to Augustus.
    2) Original thick_start/thick_stop. If we did not map over the original start/stop, do not provide them as hints.
    3) Original tss/tts. If we did not map over the original transcription start/stop, do not provide them as hints.

"""
import logging
import itertools

from toil.job import Job
from toil.common import Toil

import tools.bio
import tools.dataOps
import tools.fileOps
import tools.procOps
import tools.psl
import tools.tm2hints
import tools.transcripts
import tools.intervals
import tools.toilInterface
from tools.hintsDatabaseInterface import reflect_hints_db, get_rnaseq_hints


def augustus(args, coding_gp, toil_options):
    """
    Main entry function for Augustus toil pipeline
    :param args: dictionary of arguments from CAT
    :param coding_gp: genePred with only coding transcripts
    :param toil_options: toil options Namespace object
    """
    with Toil(toil_options) as toil:
        if not toil.options.restart:
            # assume that this fasta has been flattened
            fasta_file_ids = tools.toilInterface.write_fasta_to_filestore(toil, args['genome_fasta'])
            fasta_file_id, gdx_file_id, flat_file_id = fasta_file_ids
            input_file_ids = {'genome_fasta': fasta_file_id,
                              'genome_gdx': gdx_file_id,
                              'genome_flat': flat_file_id,
                              'tm_cfg': toil.importFile('file://' + args['tm_cfg']),
                              'coding_gp': toil.importFile('file://' + coding_gp),
                              'ref_psl': toil.importFile('file://' + args['ref_psl']),
                              'tm_psl': toil.importFile('file://' + args['tm_psl']),
                              'annotation_gp': toil.importFile('file://' + args['annotation_gp']),
                              'tm_to_hints_script': toil.importFile('file://' + args['tm_to_hints_script'])}
            if args['augustus_hints_db'] is not None:
                input_file_ids['augustus_hints_db'] = toil.importFile('file://' + args['augustus_hints_db'])
                input_file_ids['tmr_cfg'] = toil.importFile('file://' + args['tmr_cfg'])
            job = Job.wrapJobFn(setup, args, input_file_ids)
            results_file_id = toil.start(job)
        else:
            results_file_id = toil.restart()
        tools.fileOps.ensure_file_dir(args['augustus_gtf'])
        toil.exportFile(results_file_id, 'file://' + args['augustus_gtf'])


def setup(job, args, input_file_ids):
    """
    Entry function for running AugustusTM(R). Loads the genome fasta into the fileStore then spins up chunks of
    jobs.
    :param args: args from Luigi pipeline
    :param input_file_ids: file ID dictionary of imported files
    :return: completed GTF format results for all jobs
    """
    job.fileStore.logToMaster('Beginning Augustus run on {}'.format(args['genome']), level=logging.INFO)
    chunk_size = 100 if args['augustus_hints_db'] is None else 50  # RNAseq slows things down a bit
    # load all fileStore files necessary
    ref_psl = job.fileStore.readGlobalFile(input_file_ids['ref_psl'])
    tm_psl = job.fileStore.readGlobalFile(input_file_ids['tm_psl'])
    annotation_gp = job.fileStore.readGlobalFile(input_file_ids['annotation_gp'])
    coding_gp = job.fileStore.readGlobalFile(input_file_ids['coding_gp'])
    # create dictionaries of input files to split up
    ref_psl_dict = tools.psl.get_alignment_dict(ref_psl)
    tm_psl_dict = tools.psl.get_alignment_dict(tm_psl)
    ref_tx_dict = tools.transcripts.get_gene_pred_dict(annotation_gp)
    if args['augustus_hints_db'] is not None:
        job.fileStore.logToMaster('AugustusTMR loaded reference, transMap PSLs and reference genePred')
    else:
        job.fileStore.logToMaster('AugustusTM loaded reference, transMap PSLs and reference genePred')
    results = []
    gp_iter = tools.transcripts.gene_pred_iterator(coding_gp)
    for i, chunk in enumerate(tools.dataOps.grouper(gp_iter, chunk_size)):
        grouped_recs = {}
        for tx_id, tx in chunk:
            grouped_recs[tx_id] = [tx, ref_tx_dict[tools.psl.remove_alignment_number(tx_id)],
                                   tm_psl_dict[tx_id], ref_psl_dict[tools.psl.remove_alignment_number(tx_id)]]
        j = job.addChildJobFn(run_augustus_chunk, i, args, grouped_recs, input_file_ids)
        results.append(j.rv())
    return job.addFollowOnJobFn(merge, results, args).rv()


def run_augustus_chunk(job, i, args, grouped_recs, input_file_ids, padding=20000):
    """
    Runs augustus on a chunk of genePred objects.
    :param i: chunk ID. for logging.
    :param args: Arguments passed by Luigi
    :param grouped_recs: Chunk of (tx_id, GenePredTranscript) tuples
    :param input_file_ids: file ID dictionary of imported files
    :param padding: Number of bases on both side to add to Augustus run
    :return: Augustus output for this chunk
    """
    genome = args['genome']
    job.fileStore.logToMaster('Beginning chunk {} for genome {}'.format(i, genome))
    tm_to_hints_script_local_path = job.fileStore.readGlobalFile(input_file_ids['tm_to_hints_script'])
    genome_fasta = tools.toilInterface.load_fasta_from_filestore(job, input_file_ids['genome_fasta'],
                                                                 input_file_ids['genome_gdx'],
                                                                 input_file_ids['genome_flat'],
                                                                 prefix='genome', upper=False)
    job.fileStore.logToMaster('Chunk {} successfully loaded the fasta for genome {}'.format(i, genome))

    tm_cfg_file = job.fileStore.readGlobalFile(input_file_ids['tm_cfg'])
    if args['augustus_hints_db'] is not None:  # we are running TMR mode as well
        job.fileStore.logToMaster('Chunk {} is in TMR mode for genome {}'.format(i, genome))
        tmr_cfg_file = job.fileStore.readGlobalFile(input_file_ids['tmr_cfg'])
        hints_db_file = job.fileStore.readGlobalFile(input_file_ids['augustus_hints_db'])
        speciesnames, seqnames, hints, featuretypes, session = reflect_hints_db(hints_db_file)
        job.fileStore.logToMaster('Chunk {} successfully loaded the hints database for genome {}'.format(i, genome))
    else:
        job.fileStore.logToMaster('Chunk {} is in TM mode for genome {}'.format(i, genome))

    # start iteratively running Augustus on this chunk
    results = []
    for tm_tx, ref_tx, tm_psl, ref_psl in grouped_recs.itervalues():
        if len(tm_tx) > 3 * 10 ** 6:  # no huge transcripts
            continue
        chromosome = tm_tx.chromosome
        start = max(tm_tx.start - padding, 0)
        stop = min(tm_tx.stop + padding, len(genome_fasta[chromosome]))
        tm_hints = tools.tm2hints.tm_to_hints(tm_tx, tm_psl, ref_psl, tm_to_hints_script_local_path)
        if args['augustus_hints_db'] is not None:
            rnaseq_hints = get_rnaseq_hints(args['genome'], chromosome, start, stop, speciesnames, seqnames, hints,
                                            featuretypes, session)
            hint = ''.join([tm_hints, rnaseq_hints])
            transcript = run_augustus(hint, genome_fasta, tm_tx, tmr_cfg_file, start, stop,
                                       args['augustus_species'], cfg_version=2)
            if transcript is not None:
                results.extend(transcript)
        else:
            hint = tm_hints
        transcript = run_augustus(hint, genome_fasta, tm_tx, tm_cfg_file, start, stop,
                                   args['augustus_species'], cfg_version=1)
        if transcript is not None:  # we may not have found anything
            results.extend(transcript)
    return results


def run_augustus(hint, fasta, tm_tx, cfg_file, start, stop, species, cfg_version):
    """
    Runs Augustus.
    :param hint: GFF formatted hint string
    :param fasta: Pyfasta object
    :param tm_tx: GenePredTranscript object
    :param cfg_file: config file
    :param species: species parameter to pass to Augustus
    :param cfg_version: config file version
    :return: GTF formatted output from Augustus or None if nothing was produced
    """
    tmp_fasta = tools.fileOps.get_tmp_toil_file()
    tools.bio.write_fasta(tmp_fasta, tm_tx.chromosome, fasta[tm_tx.chromosome][start:stop])
    hints_out = tools.fileOps.get_tmp_toil_file()
    with open(hints_out, 'w') as outf:
        outf.write(hint)
    cmd = ['augustus', tmp_fasta, '--predictionStart=-{}'.format(start), '--predictionEnd=-{}'.format(start),
           '--extrinsicCfgFile={}'.format(cfg_file), '--hintsfile={}'.format(hints_out), '--UTR=on',
           '--alternatives-from-evidence=0', '--species={}'.format(species), '--allow_hinted_splicesites=atac',
           '--protein=0', '--softmasking=1']
    aug_output = tools.procOps.call_proc_lines(cmd)
    transcript = munge_augustus_output(aug_output, cfg_version, tm_tx)
    return transcript


def merge(job, results, args):
    """
    Merge together chain files.
    :param results: list of promises from each augustus chunk
    :param args: arguments to the pipeline
    :return:
    """
    if args['augustus_hints_db'] is None:
        job.fileStore.logToMaster('Merging AugustusTMR output for {}'.format(args['genome']), level=logging.INFO)
    else:
        job.fileStore.logToMaster('Merging AugustusTM output for {}'.format(args['genome']), level=logging.INFO)
    tmp_results_file = tools.fileOps.get_tmp_file(tmp_dir=job.fileStore.getLocalTempDir())
    # I have no idea why I have to wrap this in a list() call. Some edge case bug with print_rows()?
    tools.fileOps.print_rows(tmp_results_file, list(itertools.chain.from_iterable(results)))
    results_file_id = job.fileStore.writeGlobalFile(tmp_results_file)
    return results_file_id


# Convenience functions


def munge_augustus_output(aug_output, cfg_version, tm_tx):
    """
    Extracts transcripts from raw augustus output. If Augustus produces more than one transcript, discard all.
    Renames overlapping transcripts augIX-ID, where X is the index of the extrinsic.cfg file, e.g. 1 or 2 and where
    ID is the transMap alignment ID. Formats this transcript into a GTF string
    """
    # extract the transcript lines
    tx_entries = [x.split() for x in aug_output if "\ttranscript\t" in x]
    # filter out transcripts that do not overlap the alignment range
    valid_txs = [x[-1] for x in tx_entries if tm_tx.interval.overlap(tools.intervals.ChromosomeInterval(x[0], x[3],
                                                                                                        x[4], x[6]))]
    if len(valid_txs) != 1:
        return None
    valid_tx = valid_txs[0]
    tx_id = 'aug-I{}-{}'.format(cfg_version, tm_tx.name)
    tx_lines = [x.split('\t') for x in aug_output if valid_tx in x]
    features = {"exon", "CDS", "start_codon", "stop_codon", "tts", "tss"}
    gtf = []
    for chrom, source, feature, start, stop, score, strand, frame, attributes in tx_lines:
        if feature not in features:
            continue
        new_attributes = 'transcript_id "{}"; gene_id "{}";'.format(tx_id, tm_tx.name2)
        gtf.append([chrom, source, feature, start, stop, score, strand, frame, new_attributes])
    return gtf